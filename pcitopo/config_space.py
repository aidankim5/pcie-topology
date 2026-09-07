"""The bytes of one function's configuration space, and the little-endian readers.

[Taught] This is the step you already do by hand: pick an offset, count across
to the byte you want, and flip multi-byte fields little-endian. This module is
that and nothing else. It holds bytes and hands back numbers; no field in this
file knows what it means.

Copied from pcie-config-decoder (pcicfg/parse.py) and trimmed, not imported:
the two repos stay standalone. What was dropped is that tool's regex for
lspci's hex-row text, because this tool reads
/sys/bus/pci/devices/<BDF>/config, which is already raw bytes.

How much of configuration space a read actually returns, which is the thing to
understand before running this tool as a normal user:

- 64 bytes    the configuration header, and nothing past it. This is what the
              Linux kernel deliberately hands a process that is not root.
              In drivers/pci/pci-sysfs.c, pci_read_config() starts with
              `size = 64` and raises it to the device's full cfg_size only for
              a reader holding CAP_SYS_ADMIN. So an unprivileged read is not
              an error and not a short file: it is a policy, and it stops
              exactly at the end of the header.
- 256 bytes   everything the original PCI configuration mechanism could reach
              (spec 7.2.1). The standard capability chain lives in here.
- 4096 bytes  everything ECAM, the memory-mapped mechanism, can reach
              (spec 7.2.2). Extended capabilities start at offset 100h.

What that costs this tool: the header holds the identity fields AND the bridge
bus numbers at 18h-1Ah, so the whole tree can be built with no privileges at
all. The PCI Express Capability, which carries link speed and width, sits out
in the capability chain past 40h, so those annotations need root. The tool
says which one you are getting rather than quietly printing less.

Spec references are the PCI Express Base Specification 5.0.
"""

from dataclasses import dataclass

# Choice, not spec: the sizes a read can come back as, and what each one means.
FRAME_NAMES = {
    64: "header only, 64 bytes (an unprivileged sysfs read stops here)",
    256: "PCI-compatible space, 256 bytes (spec 7.2.1)",
    4096: "full ECAM space, 4096 bytes (spec 7.2.2)",
}

HEADER_SIZE = 64  # the configuration header, spec 7.5.1


@dataclass
class ConfigSpace:
    """The bytes of one function's configuration space plus where they came from.

    `data[off]` is the byte at absolute offset `off`. The readers below do the
    little-endian flip so callers never do it by hand.
    """

    data: bytes
    source: str = ""  # the path the bytes were read from, for messages

    # @property: call it like an attribute, cs.size, not cs.size().
    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def frame(self) -> str:
        """Which frame of configuration space these bytes cover (see the module docstring)."""
        return FRAME_NAMES.get(self.size, f"nonstandard size, {self.size} bytes")

    @property
    def header_only(self) -> bool:
        """True when the read stopped at the end of the header, as an unprivileged read does."""
        return self.size <= HEADER_SIZE

    @property
    def has_extended_space(self) -> bool:
        """True when these bytes reach past offset FFh, where extended capabilities live."""
        return self.size > 0x100

    def covers(self, off: int, width: int = 1) -> bool:
        """True when a read of `width` bytes at `off` is inside what was read.

        Callers ask this first and report "not readable at this privilege
        level" instead of raising, which is the difference between a tool that
        degrades and one that crashes.
        """
        return off >= 0 and width >= 0 and off + width <= self.size

    def _check(self, off: int, width: int) -> None:
        """Refuse a read that starts before 0, runs past the end, or has a negative width."""
        if width < 0:
            raise ValueError(f"negative width {width}")
        if not self.covers(off, width):
            raise IndexError(
                f"offset {off:#x} width {width} is outside these {self.size} bytes"
                # {off:#x} prints the offset as 0x-prefixed hex
            )

    # u8/u16/u32 = unsigned 8-, 16-, 32-bit: one, two, or four bytes read as one number.
    def u8(self, off: int) -> int:
        """The byte at absolute offset `off`."""
        self._check(off, 1)
        return self.data[off]  # indexing one position of a bytes gives an int, 0-255

    def u16(self, off: int) -> int:
        """Two bytes at `off`, little-endian: bytes `de 10` at 00h read as 0x10DE."""
        self._check(off, 2)
        # data[off:off+2] is a 2-byte slice (the end is exclusive);
        # "little" says the first byte is the low-order one.
        return int.from_bytes(self.data[off : off + 2], "little")

    def u32(self, off: int) -> int:
        """Four bytes at `off`, little-endian: bytes `04 3d 45 00` read as 0x00453D04."""
        self._check(off, 4)
        return int.from_bytes(self.data[off : off + 4], "little")

    def bytes_at(self, off: int, length: int) -> bytes:
        """`length` raw bytes starting at `off`, in dump order (no flip)."""
        self._check(off, length)
        return self.data[off : off + length]
