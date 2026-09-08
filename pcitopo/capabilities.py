"""The capability chain, and the one capability this tool came for.

[Taught] You have walked a capability chain by hand and decoded Link Status.
This module is that, done for every device at once.

[Ahead] Device/Port Type, and the fact that a link has *two* descriptions --
what it is capable of and what it actually negotiated -- which is the whole
basis of the --degraded flag.

Where a capability lives, and why it needs root:

    offset 34h     Capabilities Pointer: the offset of the first capability,
                   or 00h if there are none. Valid only when Status bit 4 is
                   set (spec 7.5.1.1.11).
    cap + 0        Capability ID
    cap + 1        the offset of the NEXT capability, 00h ends the chain
    cap + 2 ...    whatever that capability defines

Every capability sits at 40h or above (spec 7.5.1: 00h-3Fh is the header, and
the chain may not overlap it). An unprivileged sysfs read stops at 40h exactly.
So the chain is *entirely* outside what a normal user can read, and this whole
module returns nothing without sudo. That is the privilege boundary in one
sentence: identity and topology are free, link speed costs root.

Extended capabilities are the same idea one level up, starting at 100h and
reachable only through ECAM (spec 7.2.2, 7.6.3): a 32-bit header holding ID,
version and the next offset packed together instead of three separate bytes.
This tool reads them for completeness, but the register it needs is in the
standard chain.

The one it came for is the PCI Express Capability, ID 10h (spec 7.5.3). It is
the structure that makes a PCI device a PCI *Express* device, and it carries:

    cap + 02h   PCI Express Capabilities Register: Device/Port Type in bits
                7:4, which says what role this function plays in the topology
                (root port, endpoint, switch port, ...)
    cap + 0Ch   Link Capabilities:  the maximum speed and width the port
                supports -- what it is built for
    cap + 12h   Link Status:        the speed and width it actually trained to
                -- what it got

Those last two are the interesting pair. Hardware negotiates a link at reset
and may settle below the maximum for good reasons (power management parks an
idle GPU at 2.5 GT/s) or bad ones (a bent pin, a riser, a slot wired x4 that
the marketing called x16). The tool cannot tell you which, but it can tell you
that current < max, and that is the finding worth surfacing.

Copied from pcie-config-decoder (pcicfg/caps.py and pcicfg/pcie.py) and cut
down to the fields a topology needs, not imported.

Spec references are PCI Express Base Specification 5.0:
- 7.5.1.1.11  Capabilities Pointer
- 7.6.3       PCI Express Extended Capability header
- 7.5.3       PCI Express Capability Structure
- 7.5.3.2     PCI Express Capabilities Register (Device/Port Type)
- 7.5.3.6     Link Capabilities Register
- 7.5.3.8     Link Status Register
- 7.5.3.18    Link Capabilities 2 Register (Supported Link Speeds Vector)
"""

from dataclasses import dataclass, field

from .config_space import ConfigSpace
from .header import bit, bits

CAP_POINTER = 0x34
FIRST_CAP_OFFSET = 0x40  # spec 7.5.1: nothing in the chain may sit inside the header
EXTENDED_CAP_START = 0x100

# The capability IDs worth naming. Spec 7.9 assigns them; this is not the whole list,
# it is the ones that turn up on a desktop, plus the one this tool needs.
CAP_NAMES = {
    0x01: "Power Management",
    0x02: "AGP",
    0x03: "VPD",
    0x04: "Slot Identification",
    0x05: "MSI",
    0x07: "PCI-X",
    0x08: "HyperTransport",
    0x09: "Vendor-Specific",
    0x0A: "Debug Port",
    0x0D: "Subsystem ID (bridge)",
    0x0F: "Secure Device",
    0x10: "PCI Express",
    0x11: "MSI-X",
    0x12: "SATA Configuration",
    0x13: "Advanced Features",
}

EXTENDED_CAP_NAMES = {
    0x0001: "Advanced Error Reporting",
    0x0002: "Virtual Channel",
    0x0003: "Device Serial Number",
    0x0004: "Power Budgeting",
    0x0005: "Root Complex Link Declaration",
    0x000B: "Vendor-Specific Extended",
    0x000E: "Address Resolution Services",
    0x000F: "Single Root I/O Virtualization",
    0x0015: "Resizable BAR",
    0x0018: "Latency Tolerance Reporting",
    0x0019: "Secondary PCI Express",
    0x001E: "L1 PM Substates",
    0x0025: "Data Link Feature",
    0x0026: "Physical Layer 16.0 GT/s",
    0x0027: "Lane Margining at the Receiver",
    0x002A: "Physical Layer 32.0 GT/s",
    0x002F: "Device 3",
}

PCI_EXPRESS_CAP_ID = 0x10

# Spec 7.5.3.2, Table 7-19. The role this function plays in the topology.
PORT_TYPES = {
    0x0: "Endpoint",
    0x1: "Legacy Endpoint",
    0x4: "Root Port",
    0x5: "Upstream Port",
    0x6: "Downstream Port",
    0x7: "PCIe-to-PCI Bridge",
    0x8: "PCI-to-PCIe Bridge",
    0x9: "Root Complex Integrated Endpoint",
    0xA: "Root Complex Event Collector",
}

# Spec 7.5.3.6 / 7.5.3.8: the same 4-bit encoding is used for "max" and "current".
# The text matches lspci's own ("5 GT/s", not "5.0 GT/s") so the two tools' output can
# be compared line by line. Only 2.5 needs a decimal point, because only it has one.
LINK_SPEEDS = {
    1: "2.5 GT/s",
    2: "5 GT/s",
    3: "8 GT/s",
    4: "16 GT/s",
    5: "32 GT/s",
    6: "64 GT/s",
}

# Which PCIe generation each of those encodings is, for the human-readable note.
LINK_GENS = {1: "Gen1", 2: "Gen2", 3: "Gen3", 4: "Gen4", 5: "Gen5", 6: "Gen6"}

# A Device/Port Type that has no Link registers to read. A Root Complex Integrated
# Endpoint is inside the root complex silicon; there is no wire, so there is nothing
# to negotiate and the Link registers are not implemented (spec 7.5.3).
NO_LINK_PORT_TYPES = {0x9, 0xA}


def speed_text(encoding: int) -> str:
    """"16 GT/s" for 4, or a labelled unknown. 0 means no speed is reported."""
    if encoding == 0:
        return "unknown"
    return LINK_SPEEDS.get(encoding, f"reserved encoding {encoding}")


def width_text(lanes: int) -> str:
    """"x16" for 16. Width 0 is the spec's way of saying no link is up (7.5.3.8)."""
    if lanes == 0:
        return "x0 (no link)"
    return f"x{lanes}"


@dataclass
class Capability:
    """One entry in the standard chain: where it is, what it is, what follows."""

    offset: int
    cap_id: int
    next_offset: int

    @property
    def name(self) -> str:
        return CAP_NAMES.get(self.cap_id, f"unknown capability {self.cap_id:#04x}")


@dataclass
class ExtendedCapability:
    """One entry in the extended chain at 100h and up (spec 7.6.3)."""

    offset: int
    cap_id: int
    version: int
    next_offset: int

    @property
    def name(self) -> str:
        return EXTENDED_CAP_NAMES.get(self.cap_id, f"unknown extended capability {self.cap_id:#06x}")


def walk_capabilities(cs: ConfigSpace, has_list: bool) -> tuple[list[Capability], list[str]]:
    """Follow the chain from 34h. Returns the capabilities and anything wrong with the walk.

    `has_list` is Status bit 4. When it is clear, 34h is undefined and must not
    be followed at all -- reading it anyway is how a tool invents capabilities
    out of whatever byte happened to be there.

    The `seen` set is the only defence against a malformed chain that points
    back at itself; without it a loop is an infinite loop, not a bad read.
    """
    problems: list[str] = []
    if not has_list:
        return [], problems
    if not cs.covers(CAP_POINTER, 1):
        return [], ["capability chain starts at 34h, which was not read"]

    caps: list[Capability] = []
    seen: set[int] = set()
    offset = cs.u8(CAP_POINTER) & ~0x3  # spec 7.5.1.1.11: the bottom two bits are reserved

    while offset != 0:
        if offset < FIRST_CAP_OFFSET:
            problems.append(f"capability chain points at {offset:#04x}, inside the header; stopped")
            break
        if offset in seen:
            problems.append(f"capability chain loops back to {offset:#04x}; stopped")
            break
        if not cs.covers(offset, 2):
            problems.append(
                f"capability at {offset:#04x} is past the {cs.size} bytes that could be read "
                "(this is what an unprivileged read looks like); chain truncated"
            )
            break
        seen.add(offset)
        cap = Capability(offset=offset, cap_id=cs.u8(offset), next_offset=cs.u8(offset + 1) & ~0x3)
        caps.append(cap)
        offset = cap.next_offset

    return caps, problems


def walk_extended_capabilities(cs: ConfigSpace) -> list[ExtendedCapability]:
    """Follow the extended chain from 100h. Empty unless the full 4096 bytes were read.

    The header is one 32-bit word instead of three bytes (spec 7.6.3):
      bits 15:0   Capability ID
      bits 19:16  Capability Version
      bits 31:20  offset of the next header, 000h ends the chain
    All ones means ECAM answered but nothing is implemented there.
    """
    if not cs.has_extended_space:
        return []

    caps: list[ExtendedCapability] = []
    seen: set[int] = set()
    offset = EXTENDED_CAP_START

    while offset != 0 and cs.covers(offset, 4) and offset not in seen:
        seen.add(offset)
        header = cs.u32(offset)
        if header in (0, 0xFFFFFFFF):
            break  # nothing implemented here
        caps.append(
            ExtendedCapability(
                offset=offset,
                cap_id=bits(header, 15, 0),
                version=bits(header, 19, 16),
                next_offset=bits(header, 31, 20) & ~0x3,
            )
        )
        offset = caps[-1].next_offset

    return caps


@dataclass
class LinkState:
    """One direction of the pair: a speed encoding and a lane count."""

    speed: int  # the 4-bit encoding, not GT/s
    width: int  # lanes, as a plain count

    @property
    def text(self) -> str:
        return f"{speed_text(self.speed)} {width_text(self.width)}"

    @property
    def gen(self) -> str:
        return LINK_GENS.get(self.speed, "?")


@dataclass
class PcieCapability:
    """Spec 7.5.3: the PCI Express Capability, reduced to what a topology draws.

    `maximum` and `current` are the pair that matters. Either may be None: a
    Root Complex Integrated Endpoint has no link registers at all, and a
    version 1 capability structure may stop before the ones this reads.
    """

    offset: int
    version: int  # PCI Express Capabilities Register bits 3:0
    port_type: int  # bits 7:4
    slot_implemented: bool  # bit 8
    maximum: LinkState | None = None  # Link Capabilities, 0Ch
    current: LinkState | None = None  # Link Status, 12h
    supported_speeds: list[int] = field(default_factory=list)  # Link Capabilities 2, 2Ch
    link_active: bool | None = None  # Link Status bit 13, Data Link Layer Link Active
    link_training: bool | None = None  # Link Status bit 11

    @property
    def port_type_name(self) -> str:
        return PORT_TYPES.get(self.port_type, f"reserved port type {self.port_type:#x}")

    @property
    def has_link(self) -> bool:
        return self.maximum is not None and self.current is not None

    @property
    def link_up(self) -> bool:
        """A link with lanes negotiated. Width 0 is the spec's "nothing trained"."""
        return self.current is not None and self.current.width > 0

    @property
    def speed_degraded(self) -> bool:
        """Running slower than the port can go, with a link actually up."""
        if not self.has_link or not self.link_up:
            return False
        return self.current.speed < self.maximum.speed

    @property
    def width_degraded(self) -> bool:
        """Fewer lanes than the port supports, with a link actually up."""
        if not self.has_link or not self.link_up:
            return False
        return self.current.width < self.maximum.width

    @property
    def degraded(self) -> bool:
        return self.speed_degraded or self.width_degraded

    @property
    def link_text(self) -> str:
        """"16 GT/s x16 (max 16 GT/s x16)", the way the tree prints a link."""
        if not self.has_link:
            return ""
        if not self.link_up:
            return f"no link (max {self.maximum.text})"
        return f"{self.current.text} (max {self.maximum.text})"

    @property
    def degraded_reason(self) -> str:
        """Which half of the pair fell short, for the --degraded listing."""
        why = []
        if self.speed_degraded:
            why.append(
                f"speed {speed_text(self.current.speed)} of {speed_text(self.maximum.speed)}"
            )
        if self.width_degraded:
            why.append(f"width x{self.current.width} of x{self.maximum.width}")
        return ", ".join(why)


def find_pcie_capability(caps: list[Capability]) -> Capability | None:
    """The PCI Express Capability, ID 10h, or None on a plain PCI function.

    next() with a default returns the first match of a generator without
    building the whole list, and hands back None instead of raising when
    nothing matches.
    """
    return next((c for c in caps if c.cap_id == PCI_EXPRESS_CAP_ID), None)


def decode_pcie_capability(cs: ConfigSpace, cap: Capability) -> PcieCapability | None:
    """Spec 7.5.3, reduced to Device/Port Type and the two link descriptions.

    Every read is guarded by covers() because the structure's length depends on
    its version and on the port type: a version 1 capability on a device with
    no link legitimately stops before the registers below.
    """
    if not cs.covers(cap.offset + 0x02, 2):
        return None

    flags = cs.u16(cap.offset + 0x02)  # PCI Express Capabilities Register, spec 7.5.3.2
    out = PcieCapability(
        offset=cap.offset,
        version=bits(flags, 3, 0),
        port_type=bits(flags, 7, 4),
        slot_implemented=bit(flags, 8),
    )

    # Link Capabilities, spec 7.5.3.6: bits 3:0 Max Link Speed, bits 9:4 Max Link Width.
    if out.port_type not in NO_LINK_PORT_TYPES and cs.covers(cap.offset + 0x0C, 4):
        link_cap = cs.u32(cap.offset + 0x0C)
        out.maximum = LinkState(speed=bits(link_cap, 3, 0), width=bits(link_cap, 9, 4))

    # Link Status, spec 7.5.3.8: bits 3:0 Current Link Speed, bits 9:4 Negotiated Link Width.
    if out.port_type not in NO_LINK_PORT_TYPES and cs.covers(cap.offset + 0x12, 2):
        link_status = cs.u16(cap.offset + 0x12)
        out.current = LinkState(speed=bits(link_status, 3, 0), width=bits(link_status, 9, 4))
        out.link_training = bit(link_status, 11)
        out.link_active = bit(link_status, 13)

    # Link Capabilities 2, spec 7.5.3.18: bits 7:1 are a bitmap, bit N set meaning the
    # speed with encoding N is supported. Version 2 and later only, hence the guard.
    if out.version >= 2 and cs.covers(cap.offset + 0x2C, 4):
        vector = bits(cs.u32(cap.offset + 0x2C), 7, 1)
        out.supported_speeds = [n for n in range(1, 8) if bit(vector, n - 1)]

    return out
