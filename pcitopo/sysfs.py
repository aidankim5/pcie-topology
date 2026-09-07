"""Reading the live machine: /sys/bus/pci/devices/.

sysfs is a filesystem the Linux kernel makes up in memory. Nothing in it is on
a disk. Opening a file there runs kernel code that answers the question, so
"read a file" and "ask the kernel" are the same act.

The directory this tool lives in:

    /sys/bus/pci/devices/
        0000:00:01.0 -> ../../../devices/pci0000:00/0000:00:01.0
        0000:01:00.0 -> ../../../devices/pci0000:00/0000:00:01.0/0000:01:00.0

Two facts in that listing, and this module collects both:

1. The entry names are addresses, DDDD:BB:DD.F, the BDF you already know with
   a 16-bit domain in front. A domain (the kernel calls it a PCI segment) is a
   whole independent address space with its own bus 0. An ordinary desktop has
   exactly one, 0000. Machines with many CPU sockets, and Intel VMD, have more.
2. Every entry is a symlink, and the path it points at is the kernel's own
   answer to "what is this device plugged into". 0000:01:00.0 above is nested
   inside 0000:00:01.0, so the kernel believes the first hangs off the second.
   That is a second, independent source for the topology, which is why this
   module records it: the tree gets built from the bridge registers and then
   checked against these paths.

Inside each device directory, the files this tool reads:

    config              the raw configuration space, as bytes
    vendor, device      the IDs, as text, "0x10de"
    class               the 24-bit Class Code, as text, "0x030000"
    revision            the Revision ID
    subsystem_vendor, subsystem_device
    uevent              KEY=value lines the kernel would hand udev, one of which
                        is PCI_SLOT_NAME=0000:01:00.0, the same address as the
                        directory name

The text files are the kernel's decoding of bytes that are also in `config`.
Having both is a feature, not redundancy: this tool decodes the bytes itself
and compares, so a disagreement becomes a visible warning instead of a wrong
picture.

The privilege rule, which decides how much of this tool works:

    reading `config` as a normal user returns exactly 64 bytes

That is a deliberate kernel policy (pci_read_config in drivers/pci/pci-sysfs.c
starts at size = 64 and only raises it to the full size for a reader holding
CAP_SYS_ADMIN). It is not an error and there is no way to ask nicely. What
lands inside those 64 bytes is the whole configuration header: identity, the
Header Type at 0Eh, and a bridge's bus numbers at 18h-1Ah. So the entire tree
is buildable with no privileges at all. What falls outside is the capability
chain, where link speed and width live. This module records how many bytes it
actually got so the layers above can say which of the two situations you are
in rather than quietly printing less.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SYSFS_ROOT = "/sys"
DEVICES_SUBPATH = "bus/pci/devices"

# The attribute files read for every device. Each holds one hex number as text.
ATTR_NAMES = (
    "vendor",
    "device",
    "class",
    "revision",
    "subsystem_vendor",
    "subsystem_device",
)

# DDDD:BB:DD.F. The domain is at least 4 hex digits (Intel VMD uses 5, as in
# "10000:e1:00.0"), the bus is 8 bits, the device number is 5 bits so it never
# exceeds 1f, and the function is 3 bits, 0-7 (spec 7.3.2).
BDF_RE = re.compile(
    r"^(?P<domain>[0-9a-fA-F]{4,}):(?P<bus>[0-9a-fA-F]{2}):"
    r"(?P<device>[0-9a-fA-F]{2})\.(?P<function>[0-7])$"
)

# The same address as the kernel writes it into the uevent file.
UEVENT_SLOT_RE = re.compile(r"^PCI_SLOT_NAME=(?P<bdf>\S+)\s*$", re.MULTILINE)


class SysfsUnavailable(Exception):
    """There is no PCI sysfs tree to read at the given root."""


# order=True makes dataclass write the comparison methods, which sort by the fields in
# declaration order: domain, then bus, then device, then function. That is exactly the
# order lspci prints, so sorting a list of these needs no key function.
# frozen=True makes instances immutable, which also makes them usable as dict keys.
@dataclass(frozen=True, order=True)
class Address:
    """One PCI function's address: domain, bus, device, function."""

    domain: int
    bus: int
    device: int
    function: int

    @classmethod
    def parse(cls, text: str) -> "Address":
        m = BDF_RE.match(text.strip())
        if not m:
            raise ValueError(f"not a PCI address: {text!r}")
        return cls(
            domain=int(m.group("domain"), 16),
            bus=int(m.group("bus"), 16),
            device=int(m.group("device"), 16),
            function=int(m.group("function")),
        )

    def __str__(self) -> str:
        return f"{self.domain:04x}:{self.bus:02x}:{self.device:02x}.{self.function}"

    @property
    def bdf(self) -> str:
        """The address without the domain, the way lspci prints it by default."""
        return f"{self.bus:02x}:{self.device:02x}.{self.function}"

    @property
    def device_function(self) -> str:
        """Just "01.0", which is how a device is labelled inside a bus in tree output."""
        return f"{self.device:02x}.{self.function}"


@dataclass
class SysfsDevice:
    """What was read from one device directory, with nothing interpreted yet.

    This is deliberately a record of the read, not a model of the device: it
    keeps what came back, including what failed to come back. Deciding what it
    all means is model.py's job.
    """

    address: Address
    path: Path  # the entry in bus/pci/devices, e.g. /sys/bus/pci/devices/0000:01:00.0
    real_path: Path  # where that entry resolves to, which carries the kernel's nesting
    attrs: dict[str, int | None] = field(default_factory=dict)
    config: bytes = b""
    problems: list[str] = field(default_factory=list)
    # A name the source already resolved, used only when no pci.ids can name the
    # device. Empty for a live sysfs read: the kernel publishes IDs, not names.
    name_hint: str = ""

    @property
    def config_bytes_read(self) -> int:
        return len(self.config)


@dataclass
class Scan:
    """Every device directory under one sysfs root, plus anything that went wrong."""

    root: Path
    devices_dir: Path
    devices: list[SysfsDevice] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def domains(self) -> list[int]:
        """Every distinct domain seen, in order. Usually just [0]."""
        # sorted(set(...)) removes duplicates and puts them in order.
        return sorted({d.address.domain for d in self.devices})

    @property
    def config_sizes(self) -> dict[int, int]:
        """How many devices came back at each config size: {64: 26} means no root."""
        counts: dict[int, int] = {}
        for d in self.devices:
            counts[d.config_bytes_read] = counts.get(d.config_bytes_read, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def header_only(self) -> bool:
        """True when nothing came back past the 64-byte header: the unprivileged case."""
        return bool(self.devices) and all(d.config_bytes_read <= 64 for d in self.devices)


def read_hex_attr(path: Path) -> int | None:
    """One sysfs attribute file holding a hex number as text, e.g. "0x10de\\n" -> 0x10DE.

    None means the file is missing or unreadable, which is a normal thing for
    some devices and not worth an exception.
    """
    try:
        text = path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        # int(text, 0) reads the "0x" prefix and picks base 16 from it.
        return int(text, 0)
    except ValueError:
        return None


def read_config(path: Path) -> tuple[bytes, str]:
    """The device's configuration space, and a message when it is short or missing.

    Returns (bytes, problem). A 64-byte result is not a problem here; it is
    reported once for the whole scan rather than 26 times, by the caller.
    """
    try:
        data = path.read_bytes()
    except PermissionError:
        return b"", f"{path}: permission denied"
    except OSError as exc:
        # Some devices refuse a read while powered down (D3cold), which surfaces
        # as an ordinary I/O error. Losing one device beats aborting the scan.
        return b"", f"{path}: {exc.strerror or exc}"
    return data, ""


def address_from_uevent(entry: Path) -> Address | None:
    """The address out of the device's uevent file: PCI_SLOT_NAME=0000:01:00.0.

    The kernel writes this line for every PCI device, so it is a second place
    the address is stated. It is the fallback when the directory name itself
    cannot be parsed, which is the case for the checked-in fixture trees: a
    colon is an ordinary character in a Linux filename but is forbidden in one
    on Windows, so a fixture directory cannot be named 0000:01:00.0 and stay
    checkable-out on both.
    """
    try:
        text = (entry / "uevent").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    m = UEVENT_SLOT_RE.search(text)
    if not m:
        return None
    try:
        return Address.parse(m.group("bdf"))
    except ValueError:
        return None


def entry_address(entry: Path) -> tuple[Address | None, str]:
    """The address of one device directory, and a note when the two sources disagree.

    The directory name is the address, and uevent says it again. Prefer the
    name, fall back to uevent, and say so when both exist and differ.
    """
    from_name = None
    if BDF_RE.match(entry.name):
        from_name = Address.parse(entry.name)
    from_uevent = address_from_uevent(entry)

    if from_name is None:
        return from_uevent, ""
    if from_uevent is not None and from_uevent != from_name:
        return from_name, (
            f"{entry.name}: uevent says PCI_SLOT_NAME={from_uevent}, which is not the "
            "directory name; using the directory name"
        )
    return from_name, ""


def read_device(entry: Path, address: Address) -> SysfsDevice:
    """Read one device directory. Never raises for a per-device problem; records it."""
    try:
        real = entry.resolve()
    except OSError:
        real = entry
    dev = SysfsDevice(address=address, path=entry, real_path=real)

    for name in ATTR_NAMES:
        dev.attrs[name] = read_hex_attr(entry / name)

    data, problem = read_config(entry / "config")
    dev.config = data
    if problem:
        dev.problems.append(problem)
    return dev


def scan(root: str | Path = DEFAULT_SYSFS_ROOT) -> Scan:
    """Every PCI function the kernel is showing under `root`.

    `root` is normally /sys. Pointing it somewhere else is how the tests run
    with no PCI hardware at all: a fixture directory with the same shape reads
    exactly the same way, because nothing here does anything cleverer than
    listing a directory and opening files in it.
    """
    root_path = Path(root)
    devices_dir = root_path / DEVICES_SUBPATH
    if not devices_dir.is_dir():
        raise SysfsUnavailable(
            f"{devices_dir} does not exist. This tool reads the Linux PCI sysfs tree; "
            "on any other system, or to work from a captured tree, pass --sysfs-root."
        )

    result = Scan(root=root_path, devices_dir=devices_dir)
    try:
        entries = sorted(devices_dir.iterdir())
    except OSError as exc:
        raise SysfsUnavailable(f"{devices_dir}: {exc.strerror or exc}") from exc

    for entry in entries:
        if not entry.is_dir():
            continue  # a stray file in the directory is not a device
        address, note = entry_address(entry)
        if note:
            result.problems.append(note)
        if address is None:
            result.problems.append(
                f"{entry.name}: neither the directory name nor uevent gives a PCI address, skipped"
            )
            continue
        try:
            result.devices.append(read_device(entry, address))
        except OSError as exc:
            result.problems.append(f"{entry.name}: {exc.strerror or exc}")

    result.devices.sort(key=lambda d: d.address)
    if not result.devices:
        result.problems.append(f"{devices_dir} is empty: no PCI functions found")
    return result
