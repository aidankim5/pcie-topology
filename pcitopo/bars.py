"""Base Address Registers, and the windows a bridge forwards through.

[Taught] BARs and the three address spaces. You have decoded a BAR by hand.
[Ahead] The bridge side: a Type 1 header does not just sit between two buses,
it also owns *address ranges*, and anything below it is only reachable because
the bridge agreed to forward those ranges downstream.

A BAR is how a function asks for address space and then learns where it was
put. Bit 0 says which space (spec 7.5.1.2.1):

    bit 0 = 0   memory space
      bits 2:1  00 = 32-bit BAR, 10 = 64-bit BAR (it eats the next BAR too)
      bit 3     prefetchable: reads have no side effects, so a bridge upstream
                may prefetch and merge them
      bits 31:4 the base address; the low 4 bits are not part of it
    bit 0 = 1   I/O space
      bits 31:2 the base address

A BAR reading 0 is unimplemented -- not "address zero". Functions implement
only the BARs they need, and the unused ones are hardwired to zero.

The bridge windows are the same idea inverted. An endpoint's BAR says "I am
here"; a bridge's window says "everything from here to there is behind me,
send it down". Three windows, each a base/limit pair (spec 7.5.1.3):

    1Ch/1Dh   I/O Base / I/O Limit             4 KB granularity
    20h/22h   Memory Base / Memory Limit       1 MB granularity
    24h/26h   Prefetchable Base / Limit        1 MB granularity, may be 64-bit

The granularity is why the registers look too small to hold an address. The
Memory Base register is 16 bits, but only its top 12 bits are used, and they
are address bits 31:20. The bottom 20 bits of the base are implied zero and
the bottom 20 bits of the limit are implied ones, so a window always starts on
a 1 MB boundary and ends one byte before the next one. You cannot express a
window that is not 1 MB aligned, by construction.

A window with base > limit is *disabled*, which is the spec's way of saying
"this bridge forwards nothing of this kind" (spec 7.5.1.3). It is a normal
thing to see: most desktop root ports forward no I/O at all.

Copied nothing from pcie-config-decoder; that tool decodes Type 0 BARs, and
the bridge windows are new here.

Spec references are PCI Express Base Specification 5.0:
- 7.5.1.2.1  Base Address Registers (Type 0)
- 7.5.1.3    Type 1 Configuration Space Header (BARs, and the three windows)
"""

from dataclasses import dataclass

from .config_space import ConfigSpace
from .header import TYPE0, TYPE1, bit, bits

TYPE0_BAR_OFFSETS = (0x10, 0x14, 0x18, 0x1C, 0x20, 0x24)
# A bridge spends 18h-1Ah on bus numbers and 1Ch upward on windows, so it has room
# for only two BARs (spec 7.5.1.3). Most bridges implement neither.
TYPE1_BAR_OFFSETS = (0x10, 0x14)

MEM_TYPE_32 = 0b00
MEM_TYPE_64 = 0b10


@dataclass
class Bar:
    """One decoded Base Address Register."""

    index: int
    offset: int
    raw: int  # the 32-bit register, or the full 64 bits when two were joined
    is_io: bool
    is_64bit: bool
    prefetchable: bool
    base: int

    @property
    def implemented(self) -> bool:
        """A register hardwired to zero is a BAR the function does not have."""
        return self.raw != 0

    @property
    def space(self) -> str:
        return "I/O" if self.is_io else "memory"

    @property
    def text(self) -> str:
        """One line: what kind of space, how wide, and where it landed."""
        if not self.implemented:
            return "unimplemented"
        if self.is_io:
            return f"I/O at {self.base:#06x}"
        width = "64-bit" if self.is_64bit else "32-bit"
        pref = "prefetchable" if self.prefetchable else "non-prefetchable"
        return f"{width} {pref} memory at {self.base:#x}"


def decode_bars(cs: ConfigSpace, header_layout: int) -> list[Bar]:
    """Every BAR the layout defines, with 64-bit pairs joined into one entry.

    The loop cannot be a plain `for` over the offsets, because a 64-bit BAR
    consumes the register after it as its high half: that register is not a BAR
    of its own and must be skipped, or every 64-bit BAR would be followed by a
    phantom one holding the top of its address.
    """
    offsets = TYPE0_BAR_OFFSETS if header_layout == TYPE0 else TYPE1_BAR_OFFSETS
    out: list[Bar] = []
    i = 0
    while i < len(offsets):
        offset = offsets[i]
        if not cs.covers(offset, 4):
            break
        raw = cs.u32(offset)

        if bit(raw, 0):  # I/O space
            out.append(
                Bar(
                    index=i,
                    offset=offset,
                    raw=raw,
                    is_io=True,
                    is_64bit=False,
                    prefetchable=False,
                    base=raw & ~0x3,
                )
            )
            i += 1
            continue

        mem_type = bits(raw, 2, 1)
        prefetchable = bit(raw, 3)
        base = raw & ~0xF

        if mem_type == MEM_TYPE_64 and i + 1 < len(offsets) and cs.covers(offsets[i + 1], 4):
            high = cs.u32(offsets[i + 1])
            # The high register is the top 32 bits of one 64-bit address, so it is
            # shifted up rather than treated as a second address.
            base |= high << 32
            raw |= high << 32
            out.append(
                Bar(
                    index=i,
                    offset=offset,
                    raw=raw,
                    is_io=False,
                    is_64bit=True,
                    prefetchable=prefetchable,
                    base=base,
                )
            )
            i += 2  # skip the register that held the high half
            continue

        out.append(
            Bar(
                index=i,
                offset=offset,
                raw=raw,
                is_io=False,
                is_64bit=False,
                prefetchable=prefetchable,
                base=base,
            )
        )
        i += 1

    return out


@dataclass
class Window:
    """One base/limit pair: an address range a bridge forwards downstream."""

    kind: str  # "I/O", "memory", "prefetchable memory"
    base: int
    limit: int  # inclusive: the last byte inside the window
    is_64bit: bool = False

    @property
    def enabled(self) -> bool:
        """base > limit is the spec's encoding for "this window is closed"."""
        return self.base <= self.limit

    @property
    def size(self) -> int:
        return self.limit - self.base + 1 if self.enabled else 0

    @property
    def text(self) -> str:
        if not self.enabled:
            return f"{self.kind}: disabled"
        width = " (64-bit)" if self.is_64bit else ""
        return f"{self.kind}: {self.base:#x}-{self.limit:#x}{width}"


def _memory_window(kind: str, base_reg: int, limit_reg: int) -> tuple[int, int]:
    """A 16-bit base/limit pair -> the real 32-bit addresses (spec 7.5.1.3).

    Bits 15:4 of each register are address bits 31:20, so the shift is 16: up 20
    to reach bit 20, down 4 to drop the register's reserved low nibble. The
    limit's low 20 bits are implied ones because the limit is inclusive and a
    window always ends at the top of a 1 MB block.
    """
    base = (base_reg & 0xFFF0) << 16
    limit = ((limit_reg & 0xFFF0) << 16) | 0xFFFFF
    return base, limit


def decode_windows(cs: ConfigSpace, header_layout: int) -> list[Window]:
    """The three forwarding windows of a Type 1 header. Empty for an endpoint."""
    if header_layout != TYPE1:
        return []

    out: list[Window] = []

    # I/O window, 1Ch/1Dh. 4 KB granularity: bits 7:4 are address bits 15:12. The low
    # nibble is a type field, and 1 there means the real top half lives at 30h/32h.
    if cs.covers(0x1C, 2):
        io_base_reg, io_limit_reg = cs.u8(0x1C), cs.u8(0x1D)
        io_base = (io_base_reg & 0xF0) << 8
        io_limit = ((io_limit_reg & 0xF0) << 8) | 0xFFF
        io_64 = bits(io_base_reg, 3, 0) == 1
        if io_64 and cs.covers(0x30, 4):
            io_base |= cs.u16(0x30) << 16
            io_limit |= cs.u16(0x32) << 16
        out.append(Window(kind="I/O", base=io_base, limit=io_limit, is_64bit=io_64))

    # Memory window, 20h/22h. Never 64-bit: non-prefetchable memory must sit below 4 GB.
    if cs.covers(0x20, 4):
        base, limit = _memory_window("memory", cs.u16(0x20), cs.u16(0x22))
        out.append(Window(kind="memory", base=base, limit=limit))

    # Prefetchable window, 24h/26h. Low nibble 1 means 64-bit, with the upper halves
    # at 28h and 2Ch -- the same offsets a Type 0 header spends on subsystem IDs, which
    # is exactly why subsystem_ids() in header.py refuses to read them on a bridge.
    if cs.covers(0x24, 4):
        pref_base_reg, pref_limit_reg = cs.u16(0x24), cs.u16(0x26)
        base, limit = _memory_window("prefetchable memory", pref_base_reg, pref_limit_reg)
        is_64 = bits(pref_base_reg, 3, 0) == 1
        if is_64 and cs.covers(0x28, 8):
            base |= cs.u32(0x28) << 32
            limit |= cs.u32(0x2C) << 32
        out.append(Window(kind="prefetchable memory", base=base, limit=limit, is_64bit=is_64))

    return out
