"""Turn a live-machine capture into a checked-in fixture tree.

Run after booting the live Ubuntu USB and running `capture.sh`:

    python tools/import_capture.py pci-capture

It writes two fixture trees and copies the lspci answer keys:

    tests/fixtures/sysfs-capture-raptorlake/         every byte the capture holds
    tests/fixtures/sysfs-capture-raptorlake-noroot/  the same, config truncated to 64
    tests/fixtures/dumps/lspci-tv-capture.txt        `lspci -tv`, the tree answer key
    tests/fixtures/dumps/lspci-nn-capture.txt        `lspci -nn`, the identity answer key

The truncated copy is not a convenience. A non-root read of `config` returns
exactly 64 bytes (see config_space.py), so cutting the files to 64 reproduces
the unprivileged path exactly, and the test suite can assert that identity and
topology survive it while link speed does not -- without anyone having to
re-run the tool as a normal user to find out.

Directory names keep the capture's dashes (`0000-01-00.0`) rather than the
colons a live `/sys` uses, because a colon is forbidden in a Windows filename
and this repo is checked out on both. Each device's `uevent` file carries the
true address, which is where sysfs.py looks when the directory name has been
rewritten. See tests/fixtures/README.md.
"""

import shutil
import sys
from pathlib import Path

HEADER_BYTES = 64  # what an unprivileged read of `config` returns

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
FULL = FIXTURES / "sysfs-capture-raptorlake"
NOROOT = FIXTURES / "sysfs-capture-raptorlake-noroot"
DUMPS = FIXTURES / "dumps"

DEVICES_SUBPATH = Path("bus") / "pci" / "devices"


def import_capture(capture: Path) -> None:
    devices = capture / DEVICES_SUBPATH
    if not devices.is_dir():
        # capture.sh writes the nesting; a capture copied off a USB stick sometimes
        # arrives flattened, so say which layout is expected rather than just failing.
        raise SystemExit(
            f"{devices} does not exist. The capture must hold bus/pci/devices/<address>/, "
            "the layout capture.sh writes and --sysfs-root reads."
        )

    for target in (FULL, NOROOT):
        if target.exists():
            shutil.rmtree(target)
        (target / DEVICES_SUBPATH).mkdir(parents=True)

    count = 0
    for src in sorted(devices.iterdir()):
        if not src.is_dir():
            continue
        for target, truncate in ((FULL, False), (NOROOT, True)):
            dst = target / DEVICES_SUBPATH / src.name
            dst.mkdir()
            for f in sorted(src.iterdir()):
                if not f.is_file():
                    continue
                data = f.read_bytes()
                if truncate and f.name == "config":
                    data = data[:HEADER_BYTES]
                dst.joinpath(f.name).write_bytes(data)
        count += 1

    DUMPS.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in (
        ("lspci-tv.txt", "lspci-tv-capture.txt"),
        ("lspci-nn.txt", "lspci-nn-capture.txt"),
    ):
        src = capture / src_name
        if src.exists():
            shutil.copyfile(src, DUMPS / dst_name)

    sizes: dict[int, int] = {}
    for cfg in (FULL / DEVICES_SUBPATH).glob("*/config"):
        sizes[cfg.stat().st_size] = sizes.get(cfg.stat().st_size, 0) + 1

    print(f"{count} devices -> {FULL.relative_to(REPO)} and {NOROOT.relative_to(REPO)}")
    for size in sorted(sizes):
        print(f"  {sizes[size]} device(s) with {size}-byte config")
    if set(sizes) == {HEADER_BYTES}:
        print(
            "  WARNING: every config is 64 bytes, so this capture was taken without root "
            "and carries no link speed or width."
        )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tools/import_capture.py PATH-TO-CAPTURE")
    import_capture(Path(sys.argv[1]))
