"""One device, assembled from two sources that are supposed to agree.

Every identity field this tool prints exists twice on a Linux machine:

- in the configuration header, as bytes, which this tool decodes itself;
- in the sysfs attribute files (vendor, device, class, revision, ...), which
  are the kernel's decoding of those same bytes.

Reading only the attribute files would be less code. Reading only the bytes
would be more principled. Reading both and comparing is what this module does,
because a disagreement is worth seeing: it means the bytes were read at a
moment when they were not what the kernel cached, or the device is doing
something unusual, or this tool has a bug. Any of the three is better shown
than hidden. A field that disagrees is reported as a warning and the decoded
byte value is the one used, since that is the one this tool can defend.

When configuration space could not be read at all, the attribute files are
used alone and the device says so. That keeps a device that refuses a config
read from disappearing out of the tree entirely.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .config_space import ConfigSpace
from .header import CommonHeader, decode_common_header, subsystem_ids
from .sysfs import Address, Scan, SysfsDevice

# The smallest read that still contains a whole configuration header (spec 7.5.1).
MIN_USEFUL_CONFIG = 64


@dataclass
class Device:
    """One PCI function: its address, its identity, and how much is actually known."""

    address: Address
    config: ConfigSpace
    header: CommonHeader | None  # None when configuration space could not be read
    attrs: dict[str, int | None] = field(default_factory=dict)
    subsystem: tuple[int, int] | None = None  # (vendor, device), when the device reports one
    warnings: list[str] = field(default_factory=list)  # the two sources disagreed
    problems: list[str] = field(default_factory=list)  # something could not be read
    real_path: Path | None = None  # where the sysfs symlink pointed: the kernel's nesting

    # --- identity, decoded bytes preferred, attribute files as the fallback ---

    def _attr(self, name: str) -> int | None:
        return self.attrs.get(name)

    @property
    def vendor_id(self) -> int | None:
        return self.header.vendor_id if self.header else self._attr("vendor")

    @property
    def device_id(self) -> int | None:
        return self.header.device_id if self.header else self._attr("device")

    @property
    def revision(self) -> int | None:
        return self.header.revision_id if self.header else self._attr("revision")

    @property
    def class_code(self) -> int | None:
        """The 24-bit Class Code: base class, sub-class, programming interface."""
        return self.header.class_code if self.header else self._attr("class")

    @property
    def class_triple(self) -> tuple[int, int, int] | None:
        """The Class Code split the way the three bytes at 0Bh, 0Ah, 09h hold it."""
        code = self.class_code
        if code is None:
            return None
        return (code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF

    @property
    def present(self) -> bool:
        """False when the read came back all ones, meaning no function is there."""
        return self.header.function_present if self.header else self.vendor_id not in (None, 0xFFFF)

    @property
    def is_bridge(self) -> bool:
        """Type 1 header layout. Unknown configuration space is not a bridge."""
        return bool(self.header and self.header.is_bridge)

    @property
    def multi_function(self) -> bool:
        return bool(self.header and self.header.multi_function)

    @property
    def layout_name(self) -> str:
        return self.header.layout_name if self.header else "unknown (config space unreadable)"

    @property
    def config_readable(self) -> bool:
        return self.config.size > 0


def _cross_check(dev: Device, sysfs_dev: SysfsDevice) -> None:
    """Compare each decoded field against the kernel's own value; note any disagreement."""
    if dev.header is None:
        return
    checks = (
        ("vendor", "Vendor ID (00h)", dev.header.vendor_id, 4),
        ("device", "Device ID (02h)", dev.header.device_id, 4),
        ("revision", "Revision ID (08h)", dev.header.revision_id, 2),
        ("class", "Class Code (09h-0Bh)", dev.header.class_code, 6),
    )
    for attr_name, label, decoded, digits in checks:
        kernel = sysfs_dev.attrs.get(attr_name)
        if kernel is None:
            continue  # the kernel did not publish it; nothing to compare against
        if kernel != decoded:
            dev.warnings.append(
                f"{label}: decoded {decoded:#0{digits + 2}x} from config space but sysfs "
                f"reports {kernel:#0{digits + 2}x}; using the decoded value"
            )


def build_device(sysfs_dev: SysfsDevice) -> Device:
    """Turn one raw sysfs read into a Device, cross-checking as it goes."""
    cs = ConfigSpace(data=sysfs_dev.config, source=str(sysfs_dev.path / "config"))
    header = None
    problems = list(sysfs_dev.problems)

    if cs.size >= MIN_USEFUL_CONFIG:
        header = decode_common_header(cs)
    elif cs.size > 0:
        problems.append(
            f"config space returned only {cs.size} bytes, less than the {MIN_USEFUL_CONFIG}-byte "
            "header; identity taken from the sysfs attribute files instead"
        )
    else:
        problems.append("config space could not be read; identity taken from the sysfs attribute files")

    dev = Device(
        address=sysfs_dev.address,
        config=cs,
        header=header,
        attrs=dict(sysfs_dev.attrs),
        problems=problems,
        real_path=sysfs_dev.real_path,
    )

    # Subsystem IDs live in the header only for Type 0 (spec 7.5.1.2.3). For a bridge the
    # kernel gets them from a capability out past the header, so take its word for those.
    if header is not None:
        dev.subsystem = subsystem_ids(cs, header.header_layout)
    if dev.subsystem is None:
        sv, sd = sysfs_dev.attrs.get("subsystem_vendor"), sysfs_dev.attrs.get("subsystem_device")
        if sv is not None and sd is not None:
            dev.subsystem = (sv, sd)
    elif header is not None and not header.is_bridge:
        sv, sd = sysfs_dev.attrs.get("subsystem_vendor"), sysfs_dev.attrs.get("subsystem_device")
        if sv is not None and sd is not None and (sv, sd) != dev.subsystem:
            dev.warnings.append(
                f"Subsystem IDs (2Ch/2Eh): decoded {dev.subsystem[0]:04x}:{dev.subsystem[1]:04x} "
                f"but sysfs reports {sv:04x}:{sd:04x}; using the decoded value"
            )

    _cross_check(dev, sysfs_dev)
    return dev


def build_devices(result: Scan) -> list[Device]:
    """Every device in a scan, in address order."""
    return [build_device(d) for d in result.devices]
