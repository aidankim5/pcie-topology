"""Build the fixture sysfs trees under tests/fixtures/.

Why this exists: the tool reads a live Linux machine, and the tests must run
anywhere, including on a Windows laptop with no PCI sysfs at all. So the tests
read a directory that has the same shape as /sys and the same bytes in it.
Because nothing in sysfs.py does anything cleverer than listing a directory and
opening the files in it, a fixture tree exercises the real code paths.

Run it from the repo root:

    python tools/make_fixture_sysfs.py

It writes two trees:

    tests/fixtures/sysfs-raptorlake/              full 256-byte config images
    tests/fixtures/sysfs-raptorlake-noroot/       the same machine, each config
                                                  truncated to 64 bytes, which is
                                                  exactly what an unprivileged
                                                  read returns

HONESTY ABOUT THE DATA. Two devices are real: 0000:01:00.0 and 0000:02:00.0
carry the actual bytes captured with `sudo lspci -vvv -xxxx` from Aidan's
machine (tests/fixtures/dumps/). Every other device is SYNTHESIZED here from
the identity fields in that machine's `lspci -tv` listing plus plausible
register values. The synthesized bytes are correct in shape and self-consistent
-- a Type 1 header really does carry its bus numbers at 18h-1Ah -- but they are
not a recording of hardware. Anything the tests assert about a synthesized
device is a test of this tool's decoding, not evidence about real silicon.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DUMPS = REPO_ROOT / "tests" / "fixtures" / "dumps"
OUT_FULL = REPO_ROOT / "tests" / "fixtures" / "sysfs-raptorlake"
OUT_NOROOT = REPO_ROOT / "tests" / "fixtures" / "sysfs-raptorlake-noroot"

# One labelled hex row of lspci -xxxx output: "70: 00 00 00 00 ...". Same regex the
# decoder repo uses; kept here in the build tool so the package itself needs no text parsing.
HEX_ROW = re.compile(r"^\s*([0-9A-Fa-f]{2,3}):((?:\s+[0-9A-Fa-f]{2})+)\s*$")


def read_lspci_dump(path: Path) -> bytes:
    """The raw bytes out of an `lspci -vvv -xxxx` dump, ignoring its decoded text."""
    out = bytearray()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = HEX_ROW.match(line)
        if not m:
            continue
        if int(m.group(1), 16) != len(out):
            raise SystemExit(f"{path}: hex rows are not contiguous at {m.group(1)}")
        out += bytes.fromhex(" ".join(m.group(2).split()))
    if len(out) not in (64, 256, 4096):
        raise SystemExit(f"{path}: got {len(out)} bytes, expected 64, 256 or 4096")
    return bytes(out)


# --- the machine ---------------------------------------------------------------------
# Identity fields come from `lspci -tv` and `lspci -nn` on the fixture machine
# (tests/fixtures/dumps/lspci-tv-raptorlake.txt). Bus numbers come from the same
# listing: "+-01.0-[01]--" means the bridge at 00:01.0 has secondary bus 01.


@dataclass
class DeviceSpec:
    bdf: str
    vendor: int
    device: int
    class_code: int  # 24 bits: base, sub, prog-if
    revision: int = 0x01
    layout: int = 0  # 0 = Type 0 endpoint, 1 = Type 1 bridge
    multifunction: bool = False
    subsystem: tuple[int, int] = (0x1458, 0x5000)  # the motherboard, for most PCH functions
    buses: tuple[int, int, int] | None = None  # primary, secondary, subordinate (Type 1 only)
    bars: list[int] = field(default_factory=list)  # raw BAR DWORDs, low half first
    # PCI Express Capability, when the function has one:
    # (device/port type, max speed code, max width, current speed code, current width)
    express: tuple[int, int, int, int, int] | None = None
    dump: str | None = None  # a real capture in tests/fixtures/dumps/, used instead of synthesis


# Speed codes, spec 7.5.3.6 Table 7-23: 1 = 2.5, 2 = 5.0, 3 = 8.0, 4 = 16.0, 5 = 32.0 GT/s.
GEN1, GEN2, GEN3, GEN4, GEN5 = 1, 2, 3, 4, 5
# Device/Port Type, spec 7.5.3.2 bits 7:4.
EP, LEGACY_EP, ROOT_PORT, UPSTREAM, DOWNSTREAM, PCIE_TO_PCI, RCIEP = 0, 1, 4, 5, 6, 7, 9

MACHINE = [
    # --- bus 00: the root complex itself and everything built into the chipset ---
    DeviceSpec("0000:00:00.0", 0x8086, 0xA700, 0x060000, revision=0x01),
    DeviceSpec(
        "0000:00:01.0", 0x8086, 0xA70D, 0x060400, layout=1, buses=(0x00, 0x01, 0x01),
        # The GPU below this port is idle, so the link has dropped to 2.5 GT/s. That is
        # what the real 01:00.0 capture shows, so the port above it says the same.
        express=(ROOT_PORT, GEN4, 16, GEN1, 16),
    ),
    DeviceSpec(
        "0000:00:06.0", 0x8086, 0xA74D, 0x060400, layout=1, buses=(0x00, 0x02, 0x02),
        express=(ROOT_PORT, GEN4, 4, GEN4, 4),
    ),
    DeviceSpec("0000:00:0a.0", 0x8086, 0xA77D, 0x118000),
    DeviceSpec("0000:00:0e.0", 0x8086, 0xA77F, 0x010400, bars=[0xF0000004, 0x00000000]),
    DeviceSpec("0000:00:14.0", 0x8086, 0x7A60, 0x0C0330, multifunction=True, bars=[0x84400004, 0x0]),
    DeviceSpec("0000:00:14.2", 0x8086, 0x7A27, 0x050000, bars=[0x84418004, 0x0]),
    DeviceSpec("0000:00:15.0", 0x8086, 0x7A4C, 0x0C8000, multifunction=True, bars=[0x4041B000]),
    DeviceSpec("0000:00:15.1", 0x8086, 0x7A4D, 0x0C8000, bars=[0x4041C000]),
    DeviceSpec("0000:00:15.2", 0x8086, 0x7A4E, 0x0C8000, bars=[0x4041D000]),
    DeviceSpec("0000:00:16.0", 0x8086, 0x7A68, 0x078000, multifunction=True, bars=[0x84422000]),
    DeviceSpec("0000:00:17.0", 0x8086, 0x7A62, 0x010601, bars=[0x84410000, 0x84420000]),
    DeviceSpec(
        "0000:00:1a.0", 0x8086, 0x7A48, 0x060400, layout=1, buses=(0x00, 0x03, 0x03),
        # A x4 port with only x2 negotiated: the shape of "the drive is in the wrong slot".
        express=(ROOT_PORT, GEN4, 4, GEN4, 2),
    ),
    DeviceSpec(
        "0000:00:1b.0", 0x8086, 0x7A40, 0x060400, layout=1, buses=(0x00, 0x04, 0x04),
        express=(ROOT_PORT, GEN4, 4, GEN1, 0),  # nothing plugged in: no link, width 0
    ),
    DeviceSpec(
        "0000:00:1c.0", 0x8086, 0x7A38, 0x060400, layout=1, multifunction=True,
        buses=(0x00, 0x05, 0x05), express=(ROOT_PORT, GEN3, 1, GEN3, 1),
    ),
    DeviceSpec(
        "0000:00:1c.3", 0x8086, 0x7A3B, 0x060400, layout=1, buses=(0x00, 0x06, 0x06),
        express=(ROOT_PORT, GEN3, 1, GEN2, 1),  # the I226-V only ever trains to 5 GT/s
    ),
    DeviceSpec(
        "0000:00:1d.0", 0x8086, 0x7A30, 0x060400, layout=1, buses=(0x00, 0x07, 0x07),
        express=(ROOT_PORT, GEN4, 4, GEN1, 0),  # empty M.2 slot
    ),
    DeviceSpec("0000:00:1f.0", 0x8086, 0x7A04, 0x060100, multifunction=True),
    DeviceSpec("0000:00:1f.3", 0x8086, 0x7A50, 0x040300, bars=[0x8441C004, 0x0, 0x84400004, 0x0]),
    DeviceSpec("0000:00:1f.4", 0x8086, 0x7A23, 0x0C0500, bars=[0x84424000, 0x0000EFA1]),
    DeviceSpec("0000:00:1f.5", 0x8086, 0x7A24, 0x0C8000, bars=[0xFE010000]),
    # --- bus 01: the graphics card. Function 0 is a real capture. ---
    DeviceSpec("0000:01:00.0", 0x10DE, 0x2489, 0x030000, dump="rtx3060ti_01-00.0.txt"),
    DeviceSpec(
        "0000:01:00.1", 0x10DE, 0x228B, 0x040300, revision=0xA1, subsystem=(0x1458, 0x4077),
        bars=[0x85080004, 0x0], express=(LEGACY_EP, GEN4, 16, GEN1, 16),
    ),
    # --- bus 02: the NVMe drive. A real capture. ---
    DeviceSpec("0000:02:00.0", 0x144D, 0xA80C, 0x010802, dump="samsung990pro_02-00.0.txt"),
    # --- buses 03, 05, 06: the chipset-attached devices ---
    DeviceSpec(
        "0000:03:00.0", 0x1BB1, 0x5016, 0x010802, revision=0x01, subsystem=(0x1BB1, 0x5016),
        bars=[0x86000004, 0x0], express=(EP, GEN4, 4, GEN4, 2),
    ),
    DeviceSpec(
        "0000:05:00.0", 0x8086, 0x272B, 0x028000, subsystem=(0x8086, 0x0094),
        bars=[0x87000004, 0x0], express=(EP, GEN3, 1, GEN3, 1),
    ),
    DeviceSpec(
        "0000:06:00.0", 0x8086, 0x125C, 0x020000, revision=0x04, subsystem=(0x1458, 0xE000),
        bars=[0x88000004, 0x0, 0x0, 0x88100004], express=(EP, GEN3, 1, GEN2, 1),
    ),
]


def synth_config(spec: DeviceSpec) -> bytes:
    """Build a 256-byte configuration space image for one synthesized device.

    Only the registers this tool reads are filled in; everything else stays
    zero, which is what an unimplemented register reads as anyway.
    """
    buf = bytearray(256)

    def w8(off: int, value: int) -> None:
        buf[off] = value & 0xFF

    def w16(off: int, value: int) -> None:
        buf[off : off + 2] = (value & 0xFFFF).to_bytes(2, "little")

    def w32(off: int, value: int) -> None:
        buf[off : off + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")

    w16(0x00, spec.vendor)
    w16(0x02, spec.device)
    w16(0x04, 0x0007)  # Command: I/O Space, Memory Space, Bus Master enabled
    # Status bit 4 is the Capabilities List bit, and it may only be set when there is
    # actually a chain at 34h to walk (spec 7.5.1.1.11).
    w16(0x06, 0x0010 if spec.express else 0x0000)
    w8(0x08, spec.revision)
    w8(0x09, spec.class_code & 0xFF)  # programming interface
    w8(0x0A, (spec.class_code >> 8) & 0xFF)  # sub-class
    w8(0x0B, (spec.class_code >> 16) & 0xFF)  # base class
    w8(0x0C, 0x10)  # Cache Line Size, 16 DWORDs = 64 bytes
    w8(0x0E, spec.layout | (0x80 if spec.multifunction else 0x00))

    for i, value in enumerate(spec.bars):
        w32(0x10 + 4 * i, value)

    if spec.layout == 1:
        primary, secondary, subordinate = spec.buses or (0, 0, 0)
        w8(0x18, primary)
        w8(0x19, secondary)
        w8(0x1A, subordinate)
        w8(0x1B, 0x00)  # Secondary Latency Timer, hardwired 0 on PCIe
        w8(0x1C, 0xF0)  # I/O Base  -> window closed (base > limit)
        w8(0x1D, 0x00)  # I/O Limit
        w16(0x20, 0x8400)  # Memory Base  -> 84000000h
        w16(0x22, 0x850F)  # Memory Limit -> 850FFFFFh
        w16(0x24, 0x4001)  # Prefetchable Memory Base, bit 0 = 64-bit capable
        w16(0x26, 0x421F)
    else:
        w16(0x2C, spec.subsystem[0])
        w16(0x2E, spec.subsystem[1])

    if spec.express:
        port_type, max_speed, max_width, cur_speed, cur_width = spec.express
        cap = 0x40
        w8(0x34, cap)  # Capabilities Pointer
        w8(cap + 0x00, 0x10)  # Capability ID 10h = PCI Express
        w8(cap + 0x01, 0x00)  # next pointer: end of the chain
        # PCI Express Capabilities Register (7.5.3.2): version in 3:0, port type in 7:4.
        w16(cap + 0x02, 0x0002 | (port_type << 4))
        w32(cap + 0x04, 0x00008001)  # Device Capabilities, roughly plausible
        # Link Capabilities (7.5.3.6): max speed in 3:0, max width in 9:4, port number in 31:24.
        w32(cap + 0x0C, max_speed | (max_width << 4))
        # Link Status (7.5.3.8): current speed in 3:0, negotiated width in 9:4.
        w16(cap + 0x12, cur_speed | (cur_width << 4))

    return bytes(buf)


def config_for(spec: DeviceSpec) -> bytes:
    if spec.dump:
        return read_lspci_dump(DUMPS / spec.dump)
    return synth_config(spec)


def safe_dir_name(bdf: str) -> str:
    """The directory name for a fixture device.

    A real sysfs directory is named 0000:01:00.0. A colon is an ordinary
    character in a Linux filename, and forbidden in one on Windows, so a
    fixture tree named that way could not be checked out on both. The colons
    become dashes here and the real address is written into the uevent file,
    which is where the kernel publishes it too (PCI_SLOT_NAME); sysfs.py reads
    the directory name first and falls back to uevent.
    """
    return bdf.replace(":", "-")


def write_device(root: Path, spec: DeviceSpec, config: bytes) -> None:
    """One device directory, shaped like a real one in /sys/bus/pci/devices."""
    d = root / "bus" / "pci" / "devices" / safe_dir_name(spec.bdf)
    d.mkdir(parents=True, exist_ok=True)
    (d / "config").write_bytes(config)

    # The attribute files hold the kernel's own view of the same bytes, so derive them
    # from the bytes rather than from the spec: that keeps the fixture self-consistent,
    # and a deliberate mismatch can be introduced later to test the cross-check.
    vendor = int.from_bytes(config[0x00:0x02], "little")
    device = int.from_bytes(config[0x02:0x04], "little")
    class_code = config[0x09] | (config[0x0A] << 8) | (config[0x0B] << 16)
    revision = config[0x08]
    if config[0x0E] & 0x7F == 0:
        sub_vendor = int.from_bytes(config[0x2C:0x2E], "little")
        sub_device = int.from_bytes(config[0x2E:0x30], "little")
    else:
        # A bridge has no subsystem IDs in its header; the kernel reports 0000:0000
        # unless the device carries the Subsystem ID capability.
        sub_vendor = sub_device = 0x0000

    for name, value, width in (
        ("vendor", vendor, 4),
        ("device", device, 4),
        ("class", class_code, 6),
        ("revision", revision, 2),
        ("subsystem_vendor", sub_vendor, 4),
        ("subsystem_device", sub_device, 4),
    ):
        (d / name).write_text(f"0x{value:0{width}x}\n", encoding="ascii")

    # The uevent file, in the kernel's own format. PCI_SLOT_NAME carries the real
    # address, with the colons a fixture directory name cannot have.
    (d / "uevent").write_text(
        f"PCI_CLASS={class_code:X}\n"
        f"PCI_ID={vendor:04X}:{device:04X}\n"
        f"PCI_SUBSYS_ID={sub_vendor:04X}:{sub_device:04X}\n"
        f"PCI_SLOT_NAME={spec.bdf}\n"
        f"MODALIAS=pci:v{vendor:08X}d{device:08X}sv{sub_vendor:08X}sd{sub_device:08X}"
        f"bc{(class_code >> 16) & 0xFF:02X}sc{(class_code >> 8) & 0xFF:02X}i{class_code & 0xFF:02X}\n",
        encoding="ascii",
    )


def build(root: Path, truncate: int | None) -> int:
    """Write one whole tree. `truncate` caps every config image, as a non-root read does."""
    for spec in MACHINE:
        config = config_for(spec)
        if truncate is not None:
            config = config[:truncate]
        write_device(root, spec, config)
    return len(MACHINE)


def main() -> int:
    for out, truncate in ((OUT_FULL, None), (OUT_NOROOT, 64)):
        count = build(out, truncate)
        note = "full images" if truncate is None else f"truncated to {truncate} bytes"
        print(f"wrote {count} devices to {out.relative_to(REPO_ROOT)} ({note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
