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

from .bars import Bar, Window, decode_bars, decode_windows
from .bridges import Type1Header, decode_type1_header
from .capabilities import (
    Capability,
    ExtendedCapability,
    PcieCapability,
    decode_pcie_capability,
    find_pcie_capability,
    walk_capabilities,
    walk_extended_capabilities,
)
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
    bridge: Type1Header | None = None  # the bus numbers at 18h-1Ah, Type 1 headers only
    bars: list[Bar] = field(default_factory=list)  # 10h upward, layout-dependent
    windows: list[Window] = field(default_factory=list)  # bridge forwarding ranges
    caps: list[Capability] = field(default_factory=list)  # the chain from 34h, root only
    ext_caps: list[ExtendedCapability] = field(default_factory=list)  # from 100h, ECAM only
    pcie: PcieCapability | None = None  # capability 10h, where link speed lives
    attrs: dict[str, int | None] = field(default_factory=dict)
    subsystem: tuple[int, int] | None = None  # (vendor, device), when the device reports one
    warnings: list[str] = field(default_factory=list)  # the two sources disagreed
    problems: list[str] = field(default_factory=list)  # something could not be read
    real_path: Path | None = None  # where the sysfs symlink pointed: the kernel's nesting
    name_hint: str = ""  # a name the source resolved, for when pci.ids cannot

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

    # --- topology, which only a Type 1 header carries ---

    @property
    def secondary_bus(self) -> int | None:
        """The bus immediately below this bridge (19h), or None if this is not a bridge.

        The single most useful number in the tool: stage 3 builds the tree by
        matching every device's own bus against this.
        """
        return self.bridge.secondary_bus if self.bridge else None

    @property
    def subordinate_bus(self) -> int | None:
        """The highest bus number below this bridge (1Ah), or None if not a bridge."""
        return self.bridge.subordinate_bus if self.bridge else None

    def claims_bus(self, bus: int) -> bool:
        """True when `bus` falls in this bridge's [secondary, subordinate] range."""
        return bool(self.bridge and self.bridge.claims(bus))

    # --- the PCI Express capability, which needs a privileged read to reach ---

    @property
    def port_type_name(self) -> str:
        """"Root Port", "Endpoint", ... or a plain description when 10h was not readable."""
        if self.pcie:
            return self.pcie.port_type_name
        if self.header is None:
            return "unknown"
        return "PCI bridge" if self.is_bridge else "PCI function"

    @property
    def link_text(self) -> str:
        """"16 GT/s x16 (max 16 GT/s x16)", or empty when there is no link to describe."""
        return self.pcie.link_text if self.pcie else ""

    @property
    def degraded(self) -> bool:
        """The link trained below what the port supports. The finding worth surfacing."""
        return bool(self.pcie and self.pcie.degraded)

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
        name_hint=sysfs_dev.name_hint,
    )

    # Subsystem IDs live in the header only for Type 0 (spec 7.5.1.2.3). For a bridge the
    # kernel gets them from a capability out past the header, so take its word for those.
    # The bus numbers exist only in the Type 1 layout; on a Type 0 endpoint the same
    # offsets are BARs 2 and 3, so reading them here would be reading an address as a
    # bus number. The Header Type byte at 0Eh is what makes this safe to ask.
    if header is not None and header.is_bridge:
        dev.bridge = decode_type1_header(cs)
        if dev.bridge is None:
            dev.problems.append(
                "Type 1 header, but 18h-1Ah was outside the bytes that could be read; "
                "no bus numbers for this bridge"
            )

    # BARs and, for a bridge, the forwarding windows. Both live inside the 64-byte
    # header, so both survive an unprivileged read.
    if header is not None:
        dev.bars = decode_bars(cs, header.header_layout)
        dev.windows = decode_windows(cs, header.header_layout)

    # The capability chain, which does NOT survive one: every capability sits at 40h or
    # above and an unprivileged read stops at 40h exactly. walk_capabilities says so in
    # its own words rather than returning a silently short list.
    if header is not None:
        dev.caps, cap_problems = walk_capabilities(cs, header.has_capabilities_list)
        # A chain running past the end of an unprivileged read is the expected outcome,
        # not a problem worth one warning per device: render_access_note explains it
        # once for the whole scan. A chain truncated in a read that DID reach past 40h
        # is a genuine anomaly, so those are still reported.
        if not cs.header_only:
            dev.problems.extend(cap_problems)
        pcie_cap = find_pcie_capability(dev.caps)
        if pcie_cap is not None:
            dev.pcie = decode_pcie_capability(cs, pcie_cap)
        dev.ext_caps = walk_extended_capabilities(cs)

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
