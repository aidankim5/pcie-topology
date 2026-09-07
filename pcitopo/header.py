"""The configuration header: the first 64 bytes every PCI function has.

[Taught] You have decoded a Type 0 header by hand already: Vendor ID at 00h,
Device ID at 02h, the Class Code bytes at 09h-0Bh, the Capabilities Pointer at
34h, the BARs from 10h.

[Ahead] Header Type at 0Eh and what its low bits select. Every function's
first 64 bytes have the same shape up to offset 0Fh; after that the layout
depends on one byte:

    offset 0Eh, Header Type Register (spec 7.5.1.1.9, Table 7-6)
      bit 7      Multi-Function Device. 1 means this physical device
                 implements more than function 0, so functions 1-7 of the same
                 device number are worth looking for. It says nothing about
                 what the function does.
      bits 6:0   the layout of offsets 10h-3Fh:
                   0 = Type 0, an endpoint. Six BARs, subsystem IDs.
                   1 = Type 1, a bridge. Two BARs, and in the space the other
                       four BARs would have used, the bus numbers that make
                       the tree buildable.

That second half is why this module exists in a topology tool at all: the
Header Type byte is the fork in the road, and it sits inside the 64 bytes a
normal user is allowed to read.

Copied from pcie-config-decoder (pcicfg/header.py) and cut down, not imported.
That tool decodes every field of the header; this one keeps identity, the
Header Type fork, and (in bridges.py) the bus numbers, because those are what
a topology needs.

Spec references are PCI Express Base Specification 5.0:
- 7.5.1.1  Type 0/1 Common Configuration Space: 00h-0Fh, plus 34h, 3Ch, 3Dh.
           These offsets mean the same thing in both layouts.
- 7.5.1.2  Type 0 Configuration Space Header: 10h-33h, 3Eh-3Fh.
- 7.5.1.3  Type 1 Configuration Space Header: the bridge layout.

Every offset here is absolute (counted from the start of the function's
configuration space) and every multi-byte field is read little-endian through
ConfigSpace.u16/u32.
"""

from dataclasses import dataclass

from .config_space import ConfigSpace


def bit(value: int, n: int) -> bool:
    """True when bit n of value is 1.

    (value >> n) slides bit n down to position 0; & 1 keeps only that bit.
    The parentheses matter because in Python & binds looser than ==, the
    opposite of C.
    """
    return ((value >> n) & 1) == 1


def bits(value: int, hi: int, lo: int) -> int:
    """The field in bits hi:lo of value, as a number.

    (value >> lo) drops the bits below the field; the mask (1 << width) - 1 is
    `width` ones in a row, which keeps only the field. bits(0x81, 6, 0) is 1.
    """
    width = hi - lo + 1
    return (value >> lo) & ((1 << width) - 1)


# Spec 7.5.1.1.9, Table 7-6. Only 0 and 1 are defined for PCI Express; 2 was CardBus in
# older PCI and is Reserved now. Anything else is Reserved and gets named as such.
HEADER_LAYOUTS = {
    0: "Type 0 (endpoint)",
    1: "Type 1 (bridge)",
    2: "Type 2 (CardBus, reserved on PCIe)",
}

TYPE0, TYPE1 = 0, 1

# Vendor ID FFFFh is how "nothing is here" comes back on the wire: a configuration read
# to an empty slot or an unpowered function returns all ones (spec 7.5.1.1.1).
NO_FUNCTION = 0xFFFF


def decode_header_type(value: int) -> tuple[int, bool]:
    """The byte at 0Eh -> (layout, multi_function). Spec 7.5.1.1.9."""
    return bits(value, 6, 0), bit(value, 7)


@dataclass
class CommonHeader:
    """Spec 7.5.1.1: the fields that mean the same thing in both header layouts.

    Only the ones a topology tool needs. The Command and Status registers are
    decoded bit by bit in the decoder repo; here Status is kept whole, because
    exactly one of its bits matters to this tool (bit 4, Capabilities List).
    """

    vendor_id: int  # 00h, 16 bits
    device_id: int  # 02h, 16 bits
    status: int  # 06h, 16 bits, kept raw for bit 4
    revision_id: int  # 08h, 8 bits
    prog_if: int  # 09h: Class Code bits 7:0, the Programming Interface
    sub_class: int  # 0Ah: Class Code bits 15:8
    base_class: int  # 0Bh: Class Code bits 23:16
    header_type: int  # 0Eh, the raw byte
    header_layout: int  # 0Eh bits 6:0
    multi_function: bool  # 0Eh bit 7
    capabilities_pointer: int  # 34h, with bits 1:0 masked off per spec 7.5.1.1.11

    @property
    def function_present(self) -> bool:
        """Spec 7.5.1.1.1: Vendor ID FFFFh means no function is present.

        An all-ones read is the absence of a device, not device state, so it
        must never be decoded as though it were data.
        """
        return self.vendor_id != NO_FUNCTION

    @property
    def class_code(self) -> int:
        """The 24-bit Class Code as one number, base class in the top byte: 0x030000 is VGA."""
        return (self.base_class << 16) | (self.sub_class << 8) | self.prog_if

    @property
    def is_bridge(self) -> bool:
        """Type 1 layout. The one question the tree builder asks of every device."""
        return self.header_layout == TYPE1

    @property
    def has_capabilities_list(self) -> bool:
        """Status bit 4. Only when it is set may the chain at 34h be walked (spec 7.5.1.1.11)."""
        return bit(self.status, 4)

    @property
    def layout_name(self) -> str:
        # dict.get(key, default) returns default instead of raising when the key is absent.
        return HEADER_LAYOUTS.get(self.header_layout, f"reserved layout {self.header_layout}")


def decode_common_header(cs: ConfigSpace) -> CommonHeader:
    """Spec 7.5.1.1. Needs the first 64 bytes, which is what any user can read."""
    header_type = cs.u8(0x0E)
    layout, multi = decode_header_type(header_type)
    return CommonHeader(
        vendor_id=cs.u16(0x00),
        device_id=cs.u16(0x02),
        status=cs.u16(0x06),
        revision_id=cs.u8(0x08),
        # The Class Code is three separate bytes, stored low to high the way
        # everything else in configuration space is: 09h is the least significant.
        prog_if=cs.u8(0x09),
        sub_class=cs.u8(0x0A),
        base_class=cs.u8(0x0B),
        header_type=header_type,
        header_layout=layout,
        multi_function=multi,
        capabilities_pointer=cs.u8(0x34) & ~0x3,  # bottom two bits are reserved
    )


def subsystem_ids(cs: ConfigSpace, layout: int) -> tuple[int, int] | None:
    """Subsystem Vendor ID and Subsystem ID at 2Ch/2Eh, or None when the layout has none.

    Spec 7.5.1.2.3: the Device ID names the silicon, the Subsystem ID names the
    board or retail product built on it. Only Type 0 headers carry these two
    fields; on a Type 1 bridge, offsets 2Ch-2Fh are memory window registers
    instead, so reading them as subsystem IDs would be reading noise. A bridge
    that wants to report a subsystem does it through a separate capability
    (ID 0Dh), which is out past the header where root privileges are needed.
    """
    if layout != TYPE0 or not cs.covers(0x2C, 4):
        return None
    return cs.u16(0x2C), cs.u16(0x2E)
