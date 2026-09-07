"""Reading a saved `lspci -vvv -xxxx` dump back into configuration space.

[Ahead] All of it, though the idea is simple and the payoff is large.

`lspci -x` prints the raw bytes it read, as a hex dump, underneath everything
it decoded:

    00:00.0 Host bridge: Intel Corporation Raptor Lake-S Host Bridge/DRAM ...
        Subsystem: ASUSTeK Computer Inc. Device 8882
        Control: I/O- Mem+ BusMaster+ ...
    00: 86 80 00 a7 06 00 90 00 01 00 00 06 00 00 00 00
    10: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 20

Those hex rows are the same bytes `/sys/bus/pci/devices/*/config` hands over.
So a dump is a *capture*: parse the rows back into bytes and the whole rest of
this tool works on a machine it has never seen, with no hardware present and
nothing installed.

This is what makes the drag-and-drop viewer worth having. Anyone can post
`sudo lspci -vvv -xxxx > dump.txt`; almost nobody can hand over a tarball of
sysfs. The dump is the portable form of the same evidence.

**Nothing in this module decodes a register.** It rebuilds bytes and hands them
to the same `model.build_device()` that reads a live machine, so there is one
decoder in this repo and no chance of a second one drifting away from it. That
is the whole design constraint here, and the reason the parser returns a `Scan`
rather than anything of its own invention.

What is lost compared with reading sysfs, and is reported rather than hidden:

- **The cross-check.** On a live machine every identity field exists twice, in
  the bytes and in the kernel's attribute files, and model.py compares them. A
  dump has only the bytes, because lspci's own decoded lines came from those
  same bytes. One source cannot check itself, so no cross-check is claimed.
- **The kernel's nesting.** There are no symlinks in a text file, so the tree
  is built from bridge registers alone and `topology.cross_check` says so.
- **How much was captured.** `lspci -x` prints 64 bytes, `-xxx` 256, `-xxxx`
  4096. A dump taken without `-xxxx`, or without root, carries no capability
  chain, and the tool reports the same access note it would on a live
  unprivileged read -- because it is the same situation.

The decoder repo (`pcicfg`) had a regex for these hex rows. It was deliberately
dropped when helpers were copied into this repo, because sysfs hands over raw
bytes and nothing here needed it. This is that idea rebuilt for a different
reason: not to decode one device, but to accept a whole machine from a stranger.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .sysfs import Address, Scan, SysfsDevice

# A device's header line: "00:1f.3 Audio device: Intel Corporation ... (rev 11)".
# The domain is optional because plain `lspci` omits it and `lspci -D` prints it.
# The device number and function are followed by a space, which is what keeps this
# from matching a hex row -- a hex row has "00: " with the space straight after
# the colon, and no ".0" before it.
DEVICE_LINE_RE = re.compile(
    r"^(?:(?P<domain>[0-9a-fA-F]{4,}):)?"
    r"(?P<bus>[0-9a-fA-F]{2}):(?P<device>[0-9a-fA-F]{2})\.(?P<function>[0-7])"
    r"\s+(?P<description>\S.*)$"
)

# A hex row: "1f0: 40 17 1e 00 ...". Up to four offset digits, because -xxxx reaches
# fff. The bytes are separated by single spaces and there are at most 16 of them.
HEX_ROW_RE = re.compile(
    r"^(?P<offset>[0-9a-fA-F]{1,4}):\s+(?P<bytes>(?:[0-9a-fA-F]{2}[ \t]*)+)$"
)

BYTES_PER_ROW = 16


class NotAnLspciDump(Exception):
    """The text held no `lspci` device blocks with hex rows in them."""


@dataclass
class ParsedDevice:
    """One device block from the dump: its address, its bytes, and lspci's own label."""

    address: Address
    description: str  # lspci's decoded one-liner, kept as a name of last resort
    rows: dict[int, bytes] = field(default_factory=dict)  # offset -> up to 16 bytes
    problems: list[str] = field(default_factory=list)

    @property
    def has_bytes(self) -> bool:
        return bool(self.rows)

    def config(self) -> bytes:
        """The rows assembled into one contiguous image, in offset order.

        A gap would mean lspci printed a discontinuous dump, which it does not
        do; if one ever appears it is zero-filled and recorded, because a
        silent hole would decode as real register values.
        """
        if not self.rows:
            return b""
        end = max(self.rows) + len(self.rows[max(self.rows)])
        out = bytearray(end)
        covered = 0
        for offset in sorted(self.rows):
            chunk = self.rows[offset]
            out[offset : offset + len(chunk)] = chunk
            covered += len(chunk)
        if covered != end:
            self.problems.append(
                f"the hex dump has gaps: {covered} bytes printed but the last row ends at "
                f"{end:#x}; the missing bytes are zero-filled and must not be trusted"
            )
        return bytes(out)

    @property
    def name_hint(self) -> str:
        """lspci's own name for the device, e.g. "Intel Corporation Ethernet Controller I226-V".

        The description reads "Audio device: Intel Corporation Raptor Lake ... (rev 11)".
        Everything before the first colon is the class, which this tool derives from
        the bytes itself, so only the part after it is worth keeping.
        """
        _, _, rest = self.description.partition(": ")
        if not rest:
            return ""
        # Drop a trailing "(rev 11)" or "(prog-if 02 [NVM Express])": both are decoded
        # from bytes this tool already has.
        return re.sub(r"\s*\((?:rev|prog-if)\s[^)]*\)\s*$", "", rest).strip()


def parse_lspci(text: str) -> list[ParsedDevice]:
    """Split an `lspci -vvv -xxxx` dump into one ParsedDevice per device block.

    The parser is a small state machine: a device line starts a new block, a hex
    row adds bytes to the current block, and every other line is one of lspci's
    decoded lines and is ignored. Ignoring them is deliberate -- they are lspci's
    interpretation, and this tool's job is to produce its own from the bytes.
    """
    devices: list[ParsedDevice] = []
    current: ParsedDevice | None = None

    for raw_line in text.splitlines():
        # lspci indents its detail lines with a tab. A hex row and a device line
        # both start at column 0, so an indented line is never either of them.
        if raw_line[:1] in (" ", "\t"):
            continue
        line = raw_line.rstrip()
        if not line:
            continue

        row = HEX_ROW_RE.match(line)
        if row and current is not None:
            offset = int(row.group("offset"), 16)
            values = bytes(int(b, 16) for b in row.group("bytes").split())
            if len(values) > BYTES_PER_ROW:
                current.problems.append(
                    f"row at {offset:#x} has {len(values)} bytes, more than the 16 lspci prints"
                )
            current.rows[offset] = values
            continue

        header = DEVICE_LINE_RE.match(line)
        if header:
            current = ParsedDevice(
                address=Address(
                    domain=int(header.group("domain") or "0", 16),
                    bus=int(header.group("bus"), 16),
                    device=int(header.group("device"), 16),
                    function=int(header.group("function")),
                ),
                description=header.group("description"),
            )
            devices.append(current)

    return devices


def scan_from_lspci(text: str, source: str = "lspci dump") -> Scan:
    """An `lspci` dump as a Scan, so every later stage cannot tell the difference.

    Returning the same type `sysfs.scan()` returns is the point: build_devices,
    build_topology, the renderers and the exporters all take it unchanged, and
    none of them needs to know where the bytes came from.
    """
    parsed = parse_lspci(text)
    if not parsed:
        raise NotAnLspciDump(
            "no lspci device lines found. Expected the output of "
            "`sudo lspci -vvv -xxxx`, which lists each device and then its bytes as hex rows."
        )

    with_bytes = [d for d in parsed if d.has_bytes]
    if not with_bytes:
        raise NotAnLspciDump(
            f"found {len(parsed)} devices but no hex rows. The dump was taken without -x: "
            "re-run it as `sudo lspci -vvv -xxxx` so the configuration bytes are included."
        )

    root = Path(source)
    result = Scan(root=root, devices_dir=root)

    for entry in with_bytes:
        config = entry.config()
        result.devices.append(
            SysfsDevice(
                address=entry.address,
                path=root / str(entry.address),
                # No symlinks in a text file, so real_path is the entry itself and
                # topology.cross_check correctly reports that it had nothing to compare.
                real_path=root / str(entry.address),
                # No attribute files either: lspci's decoded lines came from these same
                # bytes, so they are not an independent source and are not offered as one.
                attrs={},
                config=config,
                problems=list(entry.problems),
                name_hint=entry.name_hint,
            )
        )

    skipped = len(parsed) - len(with_bytes)
    if skipped:
        result.problems.append(
            f"{skipped} device(s) in the dump had no hex rows and were left out"
        )

    result.devices.sort(key=lambda d: d.address)
    return result


def name_hints(text: str) -> dict[str, str]:
    """address -> lspci's own name for it, for use when no pci.ids is available.

    A stranger's dump decodes to raw hex IDs on a machine with no pci.ids file
    installed, which is most Windows machines. lspci already resolved those names
    on the machine the dump came from, so carrying them across costs nothing and
    keeps the tree readable.
    """
    return {
        str(d.address): d.name_hint for d in parse_lspci(text) if d.has_bytes and d.name_hint
    }
