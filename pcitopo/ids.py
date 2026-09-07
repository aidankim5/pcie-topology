"""Turning numbers into names: the pci.ids database, and a built-in fallback.

Two different things get looked up here, and they come from different places:

- Vendor and Device IDs are numbers the hardware reports about itself.
  PCI-SIG assigns Vendor IDs; each vendor then picks its own Device IDs.
  Nothing in the PCI Express spec says what 10DEh is called. That name comes
  from pci.ids, a community-maintained text file that ships with most Linux
  distributions and is the same file lspci itself reads.
- Class Codes are three bytes at offsets 09h-0Bh (spec 7.5.1.1.5). What each
  value means is assigned by PCI-SIG in the "PCI Code and ID Assignment"
  document, not in the Base spec. pci.ids carries those too, in a second
  section whose lines begin with "C ".

Where pci.ids lives, in the order this module looks:
  /usr/share/misc/pci.ids     Debian and Ubuntu (the pciutils package)
  /usr/share/hwdata/pci.ids   Fedora, Arch, openSUSE (the hwdata package)
  /usr/share/pci.ids          some minimal images
plus a .gz beside each, because some distributions ship it compressed and
gzip is in the standard library.

No network, ever. If none of those files exists, this module falls back to the
small built-in table at the bottom (enough to name the machine the fixtures
came from) and, failing that, prints the raw hex ID. Degrading to "10de:2489"
is honest; guessing a name is not.

The file format, which the parser below follows literally:

    10de  NVIDIA Corporation                 <- column 0: a vendor
    <TAB>2489  GA104 [GeForce RTX 3060 Ti]   <- one tab: a device of that vendor
    <TAB><TAB>1458 4077  Windforce OC        <- two tabs: a subsystem of that device
    C 03  Display controller                 <- column 0, "C ": a base class
    <TAB>00  VGA compatible controller       <- one tab: a sub-class
    <TAB><TAB>00  VGA controller             <- two tabs: a programming interface

Lines beginning with # are comments; blank lines are skipped.
"""

import gzip
from dataclasses import dataclass, field
from pathlib import Path

# Choice, not spec: where to look and in what order. The first readable file wins.
PCI_IDS_PATHS = (
    "/usr/share/misc/pci.ids",
    "/usr/share/hwdata/pci.ids",
    "/usr/share/pci.ids",
    "/usr/share/misc/pci.ids.gz",
    "/usr/share/hwdata/pci.ids.gz",
    "/usr/share/pci.ids.gz",
)


@dataclass
class PciIds:
    """Everything parsed out of one pci.ids file. Empty is a valid, usable state."""

    # field(default_factory=dict) gives each instance its own empty dict; a plain
    # `= {}` default would be built once and shared by every instance.
    vendors: dict[int, str] = field(default_factory=dict)
    devices: dict[tuple[int, int], str] = field(default_factory=dict)
    # (vendor, device, subsystem vendor, subsystem device) -> name
    subsystems: dict[tuple[int, int, int, int], str] = field(default_factory=dict)
    base_classes: dict[int, str] = field(default_factory=dict)
    sub_classes: dict[tuple[int, int], str] = field(default_factory=dict)
    prog_ifs: dict[tuple[int, int, int], str] = field(default_factory=dict)
    source: str = ""  # the path it was read from, or "" when nothing was found

    @property
    def loaded(self) -> bool:
        return bool(self.vendors or self.base_classes)

    # --- lookups: pci.ids first, then the built-in table, then raw hex ---

    def vendor_name(self, vendor_id: int) -> str:
        name = self.vendors.get(vendor_id) or BUILTIN_VENDORS.get(vendor_id)
        return name if name else f"{vendor_id:04x}"

    def device_name(self, vendor_id: int, device_id: int) -> str:
        name = self.devices.get((vendor_id, device_id))
        if not name:
            name = BUILTIN_DEVICES.get((vendor_id, device_id))
        return name if name else f"{device_id:04x}"

    def knows_device(self, vendor_id: int, device_id: int) -> bool:
        """True when a real name exists for this pair, rather than a hex fallback.

        device_name() always returns something printable, which makes it
        impossible for a caller to tell a name from a formatted number. This is
        the question a caller has to ask before falling back to another source.
        """
        return bool(
            self.devices.get((vendor_id, device_id))
            or BUILTIN_DEVICES.get((vendor_id, device_id))
        )

    def full_name(self, vendor_id: int, device_id: int) -> str:
        """The name the way lspci says it: vendor first, then device."""
        return f"{self.vendor_name(vendor_id)} {self.device_name(vendor_id, device_id)}"

    def subsystem_name(
        self, vendor_id: int, device_id: int, sub_vendor: int, sub_device: int
    ) -> str | None:
        """The board or retail product built on that silicon (spec 7.5.1.2.3), if named."""
        return self.subsystems.get((vendor_id, device_id, sub_vendor, sub_device))

    def class_name(self, base: int, sub: int) -> str:
        """What the top two Class Code bytes mean.

        lspci prints the sub-class name ("VGA compatible controller"), not the
        base class name ("Display controller"), so that is what this returns
        whenever the sub-class is known.
        """
        name = self.sub_classes.get((base, sub)) or BUILTIN_SUB_CLASSES.get((base, sub))
        if name:
            return name
        base_name = self.base_classes.get(base) or BUILTIN_BASE_CLASSES.get(base)
        if base_name:
            return f"{base_name} [{base:02x}{sub:02x}]"
        return f"class {base:02x}{sub:02x}"

    def prog_if_name(self, base: int, sub: int, prog_if: int) -> str | None:
        """The Programming Interface name where one is assigned: 02h under 0108h is NVM Express."""
        return self.prog_ifs.get((base, sub, prog_if)) or BUILTIN_PROG_IFS.get((base, sub, prog_if))

    def class_text(self, base: int, sub: int, prog_if: int) -> str:
        """The Class Code as one readable string; the prog-if is appended only when named."""
        text = self.class_name(base, sub)
        prog = self.prog_if_name(base, sub, prog_if)
        return f"{text} ({prog})" if prog else text

    # --- reading the file ---

    @classmethod
    def parse(cls, text: str, source: str = "") -> "PciIds":
        """Parse pci.ids text. A line that does not fit the format is skipped, not guessed at."""
        out = cls(source=source)
        in_class_section = False  # which kind of block the indented lines below belong to
        vendor_id = None
        device_id = None
        base_class = None
        sub_class = None
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            # Indentation carries the meaning here, so count the leading tabs before stripping.
            depth = len(line) - len(line.lstrip("\t"))
            body = line[depth:]
            try:
                if depth == 0 and body.startswith("C "):
                    in_class_section = True
                    code, name = body[2:].split(None, 1)
                    base_class = int(code, 16)
                    out.base_classes[base_class] = name.strip()
                    sub_class = None
                elif depth == 0:
                    in_class_section = False
                    code, name = body.split(None, 1)
                    vendor_id = int(code, 16)
                    out.vendors[vendor_id] = name.strip()
                    device_id = None
                elif depth == 1 and in_class_section and base_class is not None:
                    code, name = body.split(None, 1)
                    sub_class = int(code, 16)
                    out.sub_classes[(base_class, sub_class)] = name.strip()
                elif depth == 1 and vendor_id is not None:
                    code, name = body.split(None, 1)
                    device_id = int(code, 16)
                    out.devices[(vendor_id, device_id)] = name.strip()
                elif depth == 2 and in_class_section and sub_class is not None:
                    code, name = body.split(None, 1)
                    out.prog_ifs[(base_class, sub_class, int(code, 16))] = name.strip()
                elif depth == 2 and vendor_id is not None and device_id is not None:
                    sv, sd, name = body.split(None, 2)
                    out.subsystems[(vendor_id, device_id, int(sv, 16), int(sd, 16))] = name.strip()
            except ValueError:
                # A line that will not split into the expected pieces, or whose code is not
                # hex. Skipping the one bad line beats refusing the whole file.
                continue
        return out

    @classmethod
    def load(cls, paths: "tuple[str, ...] | None" = None) -> "PciIds":
        """The first readable pci.ids in `paths`, or an empty table when there is none."""
        for candidate in paths if paths is not None else PCI_IDS_PATHS:
            p = Path(candidate)
            try:
                if p.suffix == ".gz":
                    with gzip.open(p, "rt", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # missing, unreadable, or a directory: try the next path
            return cls.parse(text, source=str(p))
        return cls()


# --- Built-in fallback, used only when no pci.ids is installed -----------------------
# Enough to name the machine the fixtures came from, plus the classes the brief asks
# for. A courtesy table, not a database: anything absent prints as raw hex.

BUILTIN_BASE_CLASSES = {
    0x00: "Unclassified device",
    0x01: "Mass storage controller",
    0x02: "Network controller",
    0x03: "Display controller",
    0x04: "Multimedia controller",
    0x05: "Memory controller",
    0x06: "Bridge",
    0x07: "Communication controller",
    0x08: "Generic system peripheral",
    0x09: "Input device controller",
    0x0A: "Docking station",
    0x0B: "Processor",
    0x0C: "Serial bus controller",
    0x0D: "Wireless controller",
    0x0E: "Intelligent controller",
    0x0F: "Satellite communications controller",
    0x10: "Encryption controller",
    0x11: "Signal processing controller",
    0x12: "Processing accelerators",
    0x13: "Non-Essential Instrumentation",
    0xFF: "Unassigned class",
}

BUILTIN_SUB_CLASSES = {
    (0x01, 0x04): "RAID bus controller",
    (0x01, 0x06): "SATA controller",
    (0x01, 0x08): "Non-Volatile memory controller",
    (0x02, 0x00): "Ethernet controller",
    (0x02, 0x80): "Network controller",
    (0x03, 0x00): "VGA compatible controller",
    (0x04, 0x03): "Audio device",
    (0x05, 0x00): "RAM memory",
    (0x06, 0x00): "Host bridge",
    (0x06, 0x01): "ISA bridge",
    (0x06, 0x04): "PCI bridge",
    (0x07, 0x80): "Communication controller",
    (0x0C, 0x03): "USB controller",
    (0x0C, 0x05): "SMBus",
    (0x0C, 0x80): "Serial bus controller",
    (0x11, 0x80): "Signal processing controller",
}

BUILTIN_PROG_IFS = {
    (0x01, 0x06, 0x01): "AHCI 1.0",
    (0x01, 0x08, 0x02): "NVM Express",
    (0x03, 0x00, 0x00): "VGA controller",
    (0x0C, 0x03, 0x30): "XHCI",
}

BUILTIN_VENDORS = {
    0x1002: "Advanced Micro Devices, Inc. [AMD/ATI]",
    0x1022: "Advanced Micro Devices, Inc. [AMD]",
    0x1458: "Gigabyte Technology Co., Ltd",
    0x10DE: "NVIDIA Corporation",
    0x144D: "Samsung Electronics Co Ltd",
    0x1BB1: "Seagate Technology PLC",
    0x8086: "Intel Corporation",
}

BUILTIN_DEVICES = {
    (0x10DE, 0x2489): "GA104 [GeForce RTX 3060 Ti Lite Hash Rate]",
    (0x10DE, 0x228B): "GA104 High Definition Audio Controller",
    (0x144D, 0xA80C): "NVMe SSD Controller S4LV008[Pascal]",
    (0x1BB1, 0x5016): "FireCuda 520/IronWolf 525 SSD",
    (0x8086, 0x125C): "Ethernet Controller I226-V",
    (0x8086, 0x272B): "Wi-Fi 7(802.11be) AX1775*/AX1790*/BE20*/BE401/BE1750* 2x2",
    (0x8086, 0x7A04): "Z790 Chipset LPC/eSPI Controller",
    (0x8086, 0x7A23): "700 Series Chipset SMBus Controller",
    (0x8086, 0x7A24): "Raptor Lake SPI (flash) Controller",
    (0x8086, 0x7A27): "Raptor Lake PCH Shared SRAM",
    (0x8086, 0x7A30): "Raptor Lake PCI Express Root Port #9",
    (0x8086, 0x7A38): "Raptor Lake PCI Express Root Port #1",
    (0x8086, 0x7A3B): "Raptor Lake PCI Express Root Port #4",
    (0x8086, 0x7A40): "Raptor Lake PCI Express Root Port #17",
    (0x8086, 0x7A48): "Raptor Lake PCI Express Root Port #25",
    (0x8086, 0x7A4C): "Raptor Lake Serial IO I2C Host Controller #0",
    (0x8086, 0x7A4D): "Raptor Lake Serial IO I2C Host Controller #1",
    (0x8086, 0x7A4E): "Raptor Lake Serial IO I2C Host Controller #2",
    (0x8086, 0x7A50): "Raptor Lake High Definition Audio Controller",
    (0x8086, 0x7A60): "Raptor Lake USB 3.2 Gen 2x2 (20 Gb/s) XHCI Host Controller",
    (0x8086, 0x7A62): "Raptor Lake SATA AHCI Controller",
    (0x8086, 0x7A68): "Raptor Lake CSME HECI #1",
    (0x8086, 0xA700): "Raptor Lake-S Host Bridge/DRAM Controller",
    (0x8086, 0xA70D): "Raptor Lake PCI Express 5.0 Graphics Port (PEG010)",
    (0x8086, 0xA74D): "Raptor Lake PCI Express 4.0 Graphics Port",
    (0x8086, 0xA77D): "Raptor Lake Crashlog and Telemetry",
    (0x8086, 0xA77F): "RST Volume Management Device Controller",
}
