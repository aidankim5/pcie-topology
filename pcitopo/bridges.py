"""The Type 1 header: the three bytes that make a tree out of a flat list.

[Taught] Header Type at 0Eh, and that bits 6:0 == 1 selects the Type 1 layout.
[Ahead] Everything in this module. These are the first registers you have read
that describe a *relationship* instead of a device.

Stage 1 printed 27 functions in address order. Nothing in that list said what
is plugged into what, because none of the identity fields know: a Vendor ID
describes silicon, not wiring. The wiring is written in three plain bytes that
only a bridge has, in the space a Type 0 endpoint spends on BARs 2 through 5:

    offset 18h   Primary Bus Number       the bus this bridge sits ON
    offset 19h   Secondary Bus Number     the bus immediately BELOW it
    offset 1Ah   Subordinate Bus Number   the highest bus number below it

One byte each, so there is no little-endian flip to do here. That is not a
coincidence: a bus number is 8 bits wide (spec 7.3.2 gives the routing ID 8
bits of bus, 5 of device, 3 of function), so each register is exactly one byte
and the question of byte order never comes up. It is the only place in this
tool where a multi-byte read would be wrong.

What the three numbers are FOR, which is the part worth understanding:

A configuration transaction carries the bus number it is aimed at. Every
bridge compares that number against its own two boundaries and decides, with
no lookup table anywhere, whether the transaction is its business:

    target == secondary            -> claim it, and talk to a device on that
                                      bus directly (a Type 0 access)
    secondary < target <= subordinate
                                   -> forward it downstream, still addressed
                                      to a bridge further down (Type 1)
    anything else                  -> not mine, ignore it

So [secondary, subordinate] is a *range of buses this bridge is responsible
for*, and the whole PCI address space is partitioned by those ranges. That is
why the tree can be rebuilt from nothing but these bytes: a device on bus N is
below whichever bridge has secondary == N, and ranges nest because a parent's
range must contain every child's.

Firmware writes all three at boot, walking the hierarchy depth-first and
handing out bus numbers as it goes. They are ordinary read/write registers,
not hardwired: the numbers are an assignment, not an identity.

One PCI Express wrinkle worth knowing before it surprises you. Primary Bus
Number is inherited from the PCI-to-PCI Bridge spec, where it mattered for
transaction decoding. On PCI Express a Root Port or Switch Port does not need
it to route anything (spec 7.5.1.3 notes it is not used by hardware), so it is
kept for software compatibility and firmware fills it in. It is therefore the
one of the three that can be stale or wrong on real hardware without anything
breaking. This module reports it, and checks it, rather than trusting it: the
bridge's own address already says which bus it sits on.

Copied nothing from pcie-config-decoder; that tool decodes a single function
and never had a reason to read these. This is the first module in the repo
that is genuinely new work.

Spec references are PCI Express Base Specification 5.0, section 7.5.1.3 (Type 1
Configuration Space Header). The registers themselves are inherited unchanged
from the PCI-to-PCI Bridge Architecture Specification 1.2, section 3.2.5.
"""

from dataclasses import dataclass

from .config_space import ConfigSpace

# The three offsets, named so the reads below say what they mean.
PRIMARY_BUS = 0x18
SECONDARY_BUS = 0x19
SUBORDINATE_BUS = 0x1A

# The whole Type 1 bus-number block, used to ask covers() one question instead of three.
BUS_NUMBER_BLOCK = (PRIMARY_BUS, 3)

# A bus number is 8 bits (spec 7.3.2), so this is every value one can hold.
MAX_BUS = 0xFF


@dataclass
class Type1Header:
    """Spec 7.5.1.3: the bus numbers a bridge carries, and what they imply.

    Only the bus numbers. The memory and prefetchable windows at 20h-26h are
    also Type 1 registers and are also worth printing, but they describe
    address routing rather than topology, so they wait for a later stage
    (a choice, not spec: this stage is about the tree).
    """

    primary_bus: int  # 18h
    secondary_bus: int  # 19h
    subordinate_bus: int  # 1Ah

    @property
    def unconfigured(self) -> bool:
        """All three zero: firmware never assigned this bridge a range.

        Zero is a legal bus number, so this is not proof on its own. But bus 0
        is the root bus, and a bridge whose secondary is 0 would be claiming
        the bus it sits on, which cannot be true. Reset leaves all three at 0
        (spec 7.5.1.3), so this is what an unenumerated bridge looks like.
        """
        return (self.primary_bus, self.secondary_bus, self.subordinate_bus) == (0, 0, 0)

    @property
    def range_valid(self) -> bool:
        """Subordinate must be at least secondary: the range cannot run backwards.

        A bridge with subordinate < secondary claims an empty range and would
        forward nothing downstream at all, so it is a real defect and not a
        style question.
        """
        return self.subordinate_bus >= self.secondary_bus

    @property
    def bus_count(self) -> int:
        """How many bus numbers this bridge is responsible for, secondary through subordinate."""
        if not self.range_valid:
            return 0
        return self.subordinate_bus - self.secondary_bus + 1

    @property
    def is_leaf_range(self) -> bool:
        """secondary == subordinate: exactly one bus below, so nothing deeper can nest.

        True for a root port with an endpoint plugged straight into it, which
        is most of a desktop. False when a switch sits below, because a switch
        needs bus numbers of its own for its downstream ports.
        """
        return self.range_valid and self.secondary_bus == self.subordinate_bus

    def claims(self, bus: int) -> bool:
        """True when a transaction aimed at `bus` is this bridge's business.

        The routing rule from the module docstring, as one expression. Python
        chains comparisons the way mathematics does, so `a <= b <= c` is one
        test and not two, and it evaluates b only once.
        """
        return self.range_valid and self.secondary_bus <= bus <= self.subordinate_bus

    @property
    def range_text(self) -> str:
        """The bus range the way lspci writes it: "[01]" for one bus, "[04-07]" for several."""
        if not self.range_valid:
            return f"[{self.secondary_bus:02x}>{self.subordinate_bus:02x}]"  # backwards, on purpose
        if self.is_leaf_range:
            return f"[{self.secondary_bus:02x}]"
        return f"[{self.secondary_bus:02x}-{self.subordinate_bus:02x}]"


def decode_type1_header(cs: ConfigSpace) -> Type1Header | None:
    """The bus numbers at 18h-1Ah, or None when those bytes were not read.

    Returning None instead of raising is the same bargain config_space.covers()
    exists for: a caller that could not read the bytes should say so and keep
    going. In practice this never returns None on a real machine, because
    18h-1Ah is inside the 64 bytes the kernel hands any user -- which is the
    whole reason stages 1 through 3 need no sudo.

    u8 and not u16/u32: each register is one byte, so there is nothing to flip.
    """
    if not cs.covers(*BUS_NUMBER_BLOCK):
        return None
    return Type1Header(
        primary_bus=cs.u8(PRIMARY_BUS),
        secondary_bus=cs.u8(SECONDARY_BUS),
        subordinate_bus=cs.u8(SUBORDINATE_BUS),
    )
