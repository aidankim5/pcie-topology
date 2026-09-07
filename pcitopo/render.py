"""Text output. Nothing in here decodes anything; it only arranges what it is given.

Keeping rendering separate from decoding is what makes --json and --dot cheap
later: they are different renderers over the same model objects.
"""

from .ids import PciIds
from .model import Device
from .sysfs import Scan

# Choice, not spec: how a header layout is abbreviated in a column.
LAYOUT_TAGS = {0: "T0", 1: "T1"}


def layout_tag(dev: Device) -> str:
    """"T0", "T1", plus "+MF" when the Multi-Function bit is set (0Eh bit 7)."""
    if dev.header is None:
        return "?"
    tag = LAYOUT_TAGS.get(dev.header.header_layout, f"T{dev.header.header_layout}?")
    return tag + "+MF" if dev.header.multi_function else tag


def format_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Left-aligned columns sized to their contents; the last column is not padded."""
    if not rows:
        return []
    width = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            width[i] = max(width[i], len(cell))

    def line(cells: list[str]) -> str:
        # The last cell is left unpadded so no line carries trailing spaces.
        padded = [c.ljust(width[i]) for i, c in enumerate(cells[:-1])]
        return "  ".join(padded + [cells[-1]]).rstrip()

    out = [line(headers)]
    out.append("  ".join("-" * w for w in width).rstrip())
    out.extend(line(r) for r in rows)
    return out


def device_row(dev: Device, ids: PciIds) -> list[str]:
    """One device as the cells of a `list` table row."""
    vendor_id, device_id = dev.vendor_id, dev.device_id
    if vendor_id is None or device_id is None:
        ident, name = "????:????", "identity unreadable"
    elif not dev.present:
        ident, name = f"{vendor_id:04x}:{device_id:04x}", "no function present (all ones)"
    else:
        ident = f"{vendor_id:04x}:{device_id:04x}"
        name = ids.full_name(vendor_id, device_id)

    triple = dev.class_triple
    if triple is None:
        class_code, class_name = "??????", ""
    else:
        base, sub, prog_if = triple
        class_code = f"{base:02x}{sub:02x}{prog_if:02x}"
        class_name = ids.class_text(base, sub, prog_if)

    revision = "??" if dev.revision is None else f"{dev.revision:02x}"
    return [str(dev.address), ident, revision, class_code, layout_tag(dev), class_name, name]


def render_list(devices: list[Device], ids: PciIds) -> list[str]:
    """The flat listing: every function the kernel is showing, in address order."""
    headers = ["Address", "ID", "Rev", "Class", "Hdr", "Class name", "Device"]
    return format_table(headers, [device_row(d, ids) for d in devices])


def render_scan_summary(result: Scan, devices: list[Device], ids: PciIds) -> list[str]:
    """Where the data came from and how complete it is. Printed before the table."""
    out = [f"Read {len(devices)} PCI functions from {result.devices_dir}"]

    domains = result.domains
    domain_text = ", ".join(f"{d:04x}" for d in domains)
    plural = "domain" if len(domains) == 1 else "domains"
    out.append(f"{len(domains)} {plural}: {domain_text}")

    bridges = sum(1 for d in devices if d.is_bridge)
    out.append(f"{bridges} Type 1 (bridge) headers, {len(devices) - bridges} Type 0")

    if ids.loaded:
        out.append(f"Names from {ids.source}")
    else:
        out.append(
            "No pci.ids found (looked in /usr/share/misc, /usr/share/hwdata, /usr/share); "
            "using the small built-in table, otherwise raw hex IDs"
        )
    return out


def render_access_note(result: Scan) -> list[str]:
    """What the privilege level cost you, said plainly and only when it costs something."""
    sizes = result.config_sizes
    if not sizes:
        return []
    described = ", ".join(f"{count} device(s) returned {size} bytes" for size, count in sizes.items())
    out = [f"Configuration space: {described}."]
    if result.header_only:
        out += [
            "  64 bytes is the configuration header, and it is the whole of what the kernel",
            "  hands a process that is not root. Nothing is missing from the topology: the",
            "  identity fields, the Header Type at 0Eh and a bridge's bus numbers at 18h-1Ah",
            "  all sit inside those 64 bytes.",
            "  What needs root is the capability chain past offset 40h, which is where the",
            "  PCI Express Capability keeps link speed and width. Re-run with sudo for those.",
        ]
    return out


def render_problems(result: Scan, devices: list[Device]) -> list[str]:
    """Everything that could not be read, and every disagreement between the two sources."""
    out: list[str] = []
    for problem in result.problems:
        out.append(f"warning: {problem}")
    for dev in devices:
        for problem in dev.problems:
            # A plain unprivileged read is not a problem, and read_config already said so.
            out.append(f"warning: {dev.address}: {problem}")
        for warning in dev.warnings:
            out.append(f"MISMATCH: {dev.address}: {warning}")
    return out
