"""Turning the flat list into a tree, and then checking that the tree is right.

[Ahead] All of it. This is the module the whole tool exists for.

The rule, in one sentence: a device on bus N is a child of whichever bridge has
Secondary Bus Number == N.

That is the entire tree-building algorithm. It works because of what a
Secondary Bus Number *is* -- not a label, but the number a bridge compares
incoming transactions against before deciding to forward them downstream
(see bridges.py). If two things can reach bus N, exactly one of them is the
bridge that forwards to it. So "which bridge has secondary == N" has exactly
one answer, and that answer is the parent.

Three things the rule does not cover, handled below:

1. The root bus. Nothing forwards to bus 0: it is the bus the root complex
   presents directly, so every function on it is a child of the domain itself.
   The Host Bridge at 00:00.0 sits *on* bus 0 as a peer of everything else
   there; it is not their parent, which is why this module never treats it as
   one. (lspci draws it the same way: 00.0 is just another entry under [0000].)
2. Multiple domains. Bus numbers are unique only within a domain, so two
   domains can each have a bus 04 with nothing to do with each other. Every
   lookup here is keyed on (domain, bus), never on bus alone.
3. Functions. 01:00.0 and 01:00.1 are two functions of one physical device.
   They are siblings on the same bus, not parent and child, and the tree
   reflects that.

The cross-check, which is the reason this module has a second half:

sysfs already knows the answer. Each entry in /sys/bus/pci/devices is a symlink
into /sys/devices/, and the path it resolves to is nested the way the kernel
believes the hardware is:

    0000:01:00.0 -> ../../../devices/pci0000:00/0000:00:01.0/0000:01:00.0

The kernel built that nesting from the same registers, but through its own
code, at boot, from a machine that was actually there. So it is a genuinely
independent answer to the same question. This module builds the tree from the
bytes, then walks the symlink paths, then compares -- and reports a mismatch
rather than picking a winner, because a disagreement means one of the two is
wrong and the tool cannot know which.

Spec references are PCI Express Base Specification 5.0, section 7.5.1.3.
"""

from dataclasses import dataclass, field

from .model import Device
from .sysfs import BDF_RE, Address


@dataclass
class Node:
    """One device in the tree, with whatever hangs off it."""

    device: Device
    children: list["Node"] = field(default_factory=list)
    # Why this node ended up here, for the mismatch report.
    attached_by: str = "bridge secondary bus"

    @property
    def address(self) -> Address:
        return self.device.address

    def walk(self):
        """This node, then every descendant, depth first.

        `yield` makes this a generator: it hands back one node at a time
        instead of building a list, and `yield from` delegates to the child's
        own walk, which is what makes the recursion read as one line.
        """
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class DomainTree:
    """One domain: its root bus, and everything reachable from it."""

    domain: int
    roots: list[Node] = field(default_factory=list)
    orphans: list[Node] = field(default_factory=list)  # a bus no bridge claimed
    root_bus: int = 0

    def walk(self):
        for node in self.roots:
            yield from node.walk()
        for node in self.orphans:
            yield from node.walk()


@dataclass
class Topology:
    """Every domain on the machine, plus what the cross-check found."""

    domains: list[DomainTree] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_against_sysfs: bool = False

    def walk(self):
        for domain in self.domains:
            yield from domain.walk()

    @property
    def device_count(self) -> int:
        # sum over a generator counts without materialising the list.
        return sum(1 for _ in self.walk())


def _secondary_bus_owners(devices: list[Device]) -> dict[tuple[int, int], Device]:
    """(domain, secondary bus) -> the bridge that forwards to it.

    A dict, because this is the lookup the whole algorithm is one call to. If
    two bridges claim the same bus the first wins here; render.render_bus_findings
    is what reports the conflict, so this stays a lookup and not a validator.
    """
    owners: dict[tuple[int, int], Device] = {}
    for dev in devices:
        if dev.is_bridge and dev.bridge and not dev.bridge.unconfigured:
            owners.setdefault((dev.address.domain, dev.bridge.secondary_bus), dev)
    return owners


def build_topology(devices: list[Device]) -> Topology:
    """The tree, built from bus numbers alone. Nothing here reads sysfs."""
    topo = Topology()
    nodes = {dev.address: Node(device=dev) for dev in devices}
    owners = _secondary_bus_owners(devices)

    by_domain: dict[int, list[Device]] = {}
    for dev in devices:
        by_domain.setdefault(dev.address.domain, []).append(dev)

    for domain in sorted(by_domain):
        in_domain = by_domain[domain]
        # The root bus is the lowest bus number present. On every ordinary machine that
        # is 0; taking the minimum rather than assuming 0 keeps a captured tree that
        # starts higher (a VMD domain, for one) from losing its top level.
        root_bus = min(d.address.bus for d in in_domain)
        tree = DomainTree(domain=domain, root_bus=root_bus)

        for dev in sorted(in_domain, key=lambda d: d.address):
            node = nodes[dev.address]
            if dev.address.bus == root_bus:
                tree.roots.append(node)
                node.attached_by = "root bus"
                continue

            parent = owners.get((domain, dev.address.bus))
            if parent is None:
                # No bridge forwards to this bus. Rather than drop the device, hang it
                # off the domain and say so: a device that exists is a fact, and the
                # missing parent is the anomaly worth showing.
                node.attached_by = "orphan: no bridge has this as its secondary bus"
                tree.orphans.append(node)
                topo.warnings.append(
                    f"{dev.address} sits on bus {dev.address.bus:02x}, which no bridge names "
                    "as a secondary bus; attached to the domain root instead"
                )
                continue

            nodes[parent.address].children.append(node)

        topo.domains.append(tree)

    return topo


def _sysfs_parent(dev: Device) -> Address | None:
    """The parent the kernel's own directory nesting implies, or None if it says nothing.

    /sys/devices/pci0000:00/0000:00:01.0/0000:01:00.0 -> 0000:00:01.0, because the
    containing directory of a device directory is its parent device -- unless the
    containing directory is the pci0000:00 root itself, which is not a device and
    means "this sits on the root bus".
    """
    if dev.real_path is None:
        return None
    parent_name = dev.real_path.parent.name
    if not BDF_RE.match(parent_name):
        return None  # pci0000:00, or a captured tree with no nesting to read
    try:
        return Address.parse(parent_name)
    except ValueError:
        return None


def cross_check(topo: Topology, devices: list[Device]) -> None:
    """Compare the tree built from bytes against the tree the kernel published.

    Records warnings on `topo`; changes nothing. When the two disagree the tool
    prints both answers, because picking one silently is the failure mode this
    whole design is meant to avoid.
    """
    parent_of: dict[Address, Address] = {}
    for node in topo.walk():
        for child in node.children:
            parent_of[child.address] = node.address

    compared = 0
    for dev in devices:
        kernel_parent = _sysfs_parent(dev)
        if kernel_parent is None:
            continue  # nothing published for this one; not a disagreement
        compared += 1
        ours = parent_of.get(dev.address)

        if ours is None:
            topo.warnings.append(
                f"MISMATCH {dev.address}: sysfs nests it under {kernel_parent}, but no bridge "
                "claims its bus, so the bus numbers put it at the domain root"
            )
        elif ours != kernel_parent:
            topo.warnings.append(
                f"MISMATCH {dev.address}: bus numbers put it under {ours}, sysfs nests it "
                f"under {kernel_parent}; the two sources disagree and neither is assumed right"
            )

    topo.checked_against_sysfs = compared > 0
    if compared == 0:
        topo.warnings.append(
            "note: this tree could not be cross-checked against the kernel's own nesting. "
            "The sysfs entries carried no parent path, which is normal for a captured "
            "directory tree (the symlinks do not survive the copy) and not for a live /sys."
        )


def degraded_devices(topo: Topology) -> list[Device]:
    """Every device whose link trained below what its port supports, in address order."""
    return sorted(
        (n.device for n in topo.walk() if n.device.degraded),
        key=lambda d: d.address,
    )
