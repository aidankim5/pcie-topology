"""Text output. Nothing in here decodes anything; it only arranges what it is given.

Keeping rendering separate from decoding is what makes --json and --dot cheap
later: they are different renderers over the same model objects.
"""

from .capabilities import speed_text
from .ids import PciIds
from .model import Device
from .sysfs import Scan
from .topology import Node, Topology

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


# --- stage 2: the bus numbers, printed as numbers, not yet as a tree ---


def _by_domain(devices: list[Device]) -> dict[int, list[Device]]:
    """Devices grouped by domain, because bus numbers are only unique within one.

    Two domains may each have a bus 04 with nothing to do with each other, so
    every check below runs inside one domain and never across them.
    """
    out: dict[int, list[Device]] = {}
    for dev in devices:
        # setdefault(k, []) returns the existing list for k, or inserts [] and returns that.
        out.setdefault(dev.address.domain, []).append(dev)
    return out


def _buses_in_use(devices: list[Device]) -> dict[int, list[Device]]:
    """Every bus number that actually has a device sitting on it, and which devices."""
    out: dict[int, list[Device]] = {}
    for dev in devices:
        out.setdefault(dev.address.bus, []).append(dev)
    return out


def bridge_row(dev: Device, on_secondary: list[Device]) -> list[str]:
    """One bridge as the cells of a `buses` table row."""
    b = dev.bridge
    if b is None:
        return [str(dev.address), "??", "??", "??", "", "bus numbers unreadable"]

    if not on_secondary:
        # An empty slot is the ordinary reason: firmware assigns the port a bus
        # number whether or not anything is plugged into it, so the range exists
        # and the bus behind it is bare. lspci prints this as "-[04]--", nothing after.
        below = "nothing (empty slot, or nothing enumerated)"
    else:
        addresses = ", ".join(d.address.device_function for d in on_secondary)
        below = f"{len(on_secondary)}: {addresses}"

    return [
        str(dev.address),
        f"{b.primary_bus:02x}",
        f"{b.secondary_bus:02x}",
        f"{b.subordinate_bus:02x}",
        b.range_text,
        below,
    ]


def render_buses(devices: list[Device]) -> list[str]:
    """The bus-number table: one row per Type 1 header, one domain at a time."""
    out: list[str] = []
    domains = _by_domain(devices)
    headers = ["Bridge", "Pri", "Sec", "Sub", "Range", "On the secondary bus"]

    for domain in sorted(domains):
        in_domain = domains[domain]
        bridges = [d for d in in_domain if d.is_bridge]
        on_bus = _buses_in_use(in_domain)

        if len(domains) > 1:
            out.append(f"Domain {domain:04x}")
        if not bridges:
            out.append("no Type 1 headers: every function sits on the root bus")
            continue

        rows = [bridge_row(d, on_bus.get(d.secondary_bus, [])) for d in bridges]
        out.extend(format_table(headers, rows))
        out.append("")

        root_bus = min(on_bus) if on_bus else 0
        claimed = {
            bus
            for d in bridges
            if d.bridge and d.bridge.range_valid
            for bus in range(d.bridge.secondary_bus, d.bridge.subordinate_bus + 1)
        }
        out.append(
            f"{len(bridges)} bridges claim {len(claimed)} bus number(s) between them. "
            f"Bus {root_bus:02x} is the root bus: it is reached without crossing a Type 1 "
            "header at all, which is why no row above names it."
        )
    return out


def render_bus_findings(devices: list[Device]) -> list[str]:
    """Every check these three bytes can be held to, and what a failure would mean.

    These are the invariants stage 3 is about to depend on. Checking them while
    the numbers are still printed as numbers is the point of this stage: a tree
    drawn from bad bus numbers looks exactly as convincing as a good one.
    """
    out: list[str] = []
    domains = _by_domain(devices)

    for domain, in_domain in sorted(domains.items()):
        bridges = [d for d in in_domain if d.is_bridge and d.bridge]
        on_bus = _buses_in_use(in_domain)
        prefix = f"{domain:04x}:" if len(domains) > 1 else ""

        # 1. The range must not run backwards.
        for dev in bridges:
            if not dev.bridge.range_valid:
                out.append(
                    f"BROKEN  {dev.address}: subordinate {dev.bridge.subordinate_bus:02x} is "
                    f"below secondary {dev.bridge.secondary_bus:02x}; this bridge forwards nothing"
                )

        # 2. Never enumerated. Legal bytes, but they cannot describe a real position.
        for dev in bridges:
            if dev.bridge.unconfigured:
                out.append(
                    f"UNCONFIGURED  {dev.address}: all three bus numbers are 00, so firmware "
                    "never gave this bridge a range; nothing below it is reachable"
                )

        # 3. Primary Bus Number should name the bus the bridge itself sits on.
        for dev in bridges:
            if dev.bridge.unconfigured:
                continue
            if dev.bridge.primary_bus != dev.address.bus:
                out.append(
                    f"NOTE  {dev.address}: Primary Bus Number (18h) says "
                    f"{dev.bridge.primary_bus:02x} but this bridge sits on bus "
                    f"{dev.address.bus:02x}. PCI Express hardware does not route with this "
                    "register, so a stale value breaks nothing; the address is the one to believe"
                )

        # 4. Two bridges cannot both own the same bus.
        secondaries: dict[int, list[Device]] = {}
        for dev in bridges:
            secondaries.setdefault(dev.bridge.secondary_bus, []).append(dev)
        for bus, owners in sorted(secondaries.items()):
            if len(owners) > 1:
                names = ", ".join(str(d.address) for d in owners)
                out.append(
                    f"CONFLICT  bus {prefix}{bus:02x} is the secondary bus of {len(owners)} "
                    f"bridges at once ({names}); a device on it would have no single parent"
                )

        # 5. A populated bus that no bridge points down at. The root bus is the exception
        #    by definition: it is reached without traversing a bridge.
        root_bus = min(on_bus) if on_bus else 0
        for bus in sorted(on_bus):
            if bus == root_bus or bus in secondaries:
                continue
            names = ", ".join(str(d.address) for d in on_bus[bus])
            out.append(
                f"ORPHAN  bus {prefix}{bus:02x} has devices on it ({names}) but no bridge names "
                "it as a secondary bus; stage 3 would have nowhere to attach them"
            )

        # 6. Ranges must nest: a bridge inside another's range must fit entirely inside it.
        for outer in bridges:
            for inner in bridges:
                if inner is outer or inner.bridge.unconfigured or not inner.bridge.range_valid:
                    continue
                if not outer.bridge.claims(inner.address.bus):
                    continue  # the inner bridge is not below this one at all
                if not (
                    outer.bridge.claims(inner.bridge.secondary_bus)
                    and outer.bridge.claims(inner.bridge.subordinate_bus)
                ):
                    out.append(
                        f"BROKEN  {inner.address} {inner.bridge.range_text} sits below "
                        f"{outer.address} {outer.bridge.range_text} but its range is not "
                        "contained in it; a transaction for the child would never be forwarded"
                    )

    if not out:
        return [
            "All checks pass: every range runs forwards, no two bridges claim the same bus,",
            "every populated bus above the root is some bridge's secondary bus, and no child",
            "range escapes its parent. Stage 3 has a sound foundation to build the tree on.",
        ]
    return out


# --- stages 3 and 4: the tree, with the link annotations on every node ---

# Choice, not spec: the box-drawing pieces. Kept in one place so an --ascii mode
# would be a dict swap rather than a rewrite of the walker.
BRANCH = "\u251c\u2500 "  # a child with siblings after it
LAST = "\u2514\u2500 "  # the last child
THROUGH = "\u2502  "  # a sibling line passing a deeper level
BLANK = "   "


def node_headline(dev: Device, ids: PciIds) -> str:
    """The first line of a node: address, what it is, and what it is called."""
    vendor_id, device_id = dev.vendor_id, dev.device_id
    if vendor_id is None or device_id is None:
        name = "identity unreadable"
    elif not dev.present:
        name = "no function present"
    else:
        name = ids.full_name(vendor_id, device_id)

    parts = [f"[{dev.address}]", dev.port_type_name]
    if dev.bridge and not dev.bridge.unconfigured:
        parts.append(dev.bridge.range_text)
    parts.append(name)
    return "  ".join(parts)


def node_detail(dev: Device, ids: PciIds) -> list[str]:
    """The continuation lines: class, link, and the degraded verdict."""
    out: list[str] = []
    triple = dev.class_triple
    if triple is not None:
        base, sub, prog_if = triple
        out.append(f"{ids.class_text(base, sub, prog_if)} [{base:02x}{sub:02x}{prog_if:02x}]")

    link = dev.link_text
    if link:
        if dev.degraded:
            # The finding the brief asks to flag. Saying which half fell short is the
            # difference between a warning and a diagnosis.
            out.append(f"{link}  DEGRADED: {dev.pcie.degraded_reason}")
        else:
            out.append(link)
    elif dev.pcie is None and dev.config.header_only:
        out.append("link speed unknown: the capability chain needs a privileged read")
    return out


def node_annotations(dev: Device, ids: PciIds) -> list[str]:
    """Everything --verbose adds: BARs, bridge windows, and the capability chain."""
    out: list[str] = []
    for bar in dev.bars:
        if bar.implemented:
            out.append(f"BAR{bar.index} ({bar.offset:#04x}): {bar.text}")
    for window in dev.windows:
        out.append(f"window {window.text}")
    if dev.caps:
        chain = ", ".join(f"{c.name} @{c.offset:#04x}" for c in dev.caps)
        out.append(f"capabilities: {chain}")
    if dev.ext_caps:
        chain = ", ".join(f"{c.name} @{c.offset:#05x}" for c in dev.ext_caps)
        out.append(f"extended: {chain}")
    if dev.pcie and dev.pcie.supported_speeds:
        speeds = ", ".join(speed_text(s) for s in dev.pcie.supported_speeds)
        out.append(f"port supports: {speeds}")
    return out


def _render_node(node: "Node", ids: PciIds, prefix: str, is_last: bool, verbose: bool) -> list[str]:
    """One node and everything under it. `prefix` is the drawing to its left.

    The recursion carries the prefix down rather than the depth, because what a
    deeper line needs is not "how far in" but "which ancestors still have
    siblings below them" -- that is what decides whether a vertical bar or a
    gap is drawn at each level.
    """
    connector = LAST if is_last else BRANCH
    out = [prefix + connector + node_headline(node.device, ids)]

    # Continuation lines sit under the text, not under the connector, so they line up
    # with the name above them instead of looking like children.
    below = prefix + (BLANK if is_last else THROUGH)
    # Two spaces past the address, so a detail line starts under the port type in the
    # headline above it rather than under the bracket.
    detail_indent = below + " " * (len(str(node.device.address)) + 4)
    for line in node_detail(node.device, ids):
        out.append(detail_indent + line)
    if verbose:
        for line in node_annotations(node.device, ids):
            out.append(detail_indent + line)

    for i, child in enumerate(node.children):
        out.extend(_render_node(child, ids, below, i == len(node.children) - 1, verbose))
    return out


def render_tree(topo: "Topology", ids: PciIds, verbose: bool = False) -> list[str]:
    """The whole topology, one domain at a time."""
    out: list[str] = []
    for tree in topo.domains:
        out.append(f"Domain {tree.domain:04x}  (root bus {tree.root_bus:02x})")
        entries = tree.roots + tree.orphans
        for i, node in enumerate(entries):
            out.extend(_render_node(node, ids, "", i == len(entries) - 1, verbose))
        out.append("")
    return out


def render_topology_warnings(topo: "Topology") -> list[str]:
    """What the cross-check against the kernel's own nesting found."""
    return list(topo.warnings)


def render_degraded(devices: list[Device], ids: PciIds) -> list[str]:
    """Only the links running below their maximum, with why.

    A degraded link is not automatically a fault, and the tool says so rather
    than implying a problem it cannot diagnose: an idle GPU parks itself at
    2.5 GT/s on purpose, and a root port that can do more than the card plugged
    into it will always read as degraded. What the tool can honestly report is
    the discrepancy.
    """
    rows = []
    for dev in devices:
        if not dev.degraded:
            continue
        vendor_id, device_id = dev.vendor_id, dev.device_id
        name = ids.full_name(vendor_id, device_id) if vendor_id and device_id else "?"
        rows.append(
            [
                str(dev.address),
                dev.port_type_name,
                dev.pcie.current.text,
                dev.pcie.maximum.text,
                name,
            ]
        )

    if not rows:
        return ["No degraded links: every link that is up trained to its full speed and width."]

    out = format_table(["Address", "Port type", "Current", "Maximum", "Device"], rows)
    out.append("")
    out += [
        f"{len(rows)} link(s) below maximum. Not every one is a fault:",
        "  - a link with no device plugged in reports width 0 and is skipped here;",
        "  - power management parks an idle device at 2.5 GT/s and raises it on demand;",
        "  - a port faster than the card in it is a property of the pair, not a defect.",
        "  What is worth investigating is a device that should be busy and is not at full rate.",
    ]
    return out
