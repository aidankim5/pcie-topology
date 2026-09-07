"""Command line.

Built so far (stage 1): `list`, the flat inventory of every PCI function the
kernel is showing, with the identity fields decoded from configuration space
and cross-checked against the kernel's own attribute files.

Not built yet: `tree`, which is the point of the tool. It says so rather than
printing something half-right.

Exit codes (a choice, not spec, and the same ones the decoder repo uses):
  0  done
  1  the sysfs tree is missing or unreadable
  2  usage error (argparse's own convention: unknown command or flag)
  3  the requested part of the tool is not built yet
"""

import argparse
import sys

from . import __version__
from .ids import PciIds
from .model import build_devices
from .render import render_access_note, render_list, render_problems, render_scan_summary
from .sysfs import DEFAULT_SYSFS_ROOT, SysfsUnavailable, scan

OK, BAD_INPUT, NOT_YET = 0, 1, 3  # 2 is taken by argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pcitopo",
        description="Draw the PCI Express topology of a live Linux machine.",
    )
    p.add_argument("--version", action="version", version=f"pcitopo {__version__}")

    # One sub-command per verb: `pcitopo list`, later `pcitopo tree`.
    # dest="cmd" stores which one was typed; required=True refuses none.
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--sysfs-root",
            default=DEFAULT_SYSFS_ROOT,
            metavar="PATH",
            help=(
                "where the sysfs tree is mounted (default: %(default)s). Point this at a "
                "captured copy to work without the hardware present."
            ),
        )
        sp.add_argument(
            "--ids",
            metavar="PATH",
            help=(
                "a pci.ids file to read names from, instead of searching the usual "
                "system locations"
            ),
        )

    lst = sub.add_parser(
        "list",
        help="every PCI function, flat, with its identity fields",
        description=(
            "List every PCI function under /sys/bus/pci/devices, in address order. "
            "Identity is decoded from configuration space and checked against the "
            "kernel's own attribute files; any disagreement is printed."
        ),
    )
    add_common(lst)

    tree = sub.add_parser(
        "tree",
        help="the topology as a tree (not built yet)",
        description="Not built yet: stage 3 of the build order.",
    )
    add_common(tree)
    return p


def load_ids(path: str | None) -> PciIds:
    """The name database: the file the user named, else the usual system locations."""
    return PciIds.load((path,) if path else None)


def cmd_list(args: argparse.Namespace) -> int:
    try:
        result = scan(args.sysfs_root)
    except SysfsUnavailable as exc:
        print(f"pcitopo: {exc}", file=sys.stderr)
        return BAD_INPUT

    ids = load_ids(args.ids)
    devices = build_devices(result)

    for line in render_scan_summary(result, devices, ids):
        print(line)
    for line in render_access_note(result):
        print(line)
    print()
    for line in render_list(devices, ids):
        print(line)

    problems = render_problems(result, devices)
    if problems:
        print()
        for line in problems:
            print(line, file=sys.stderr)
    return OK


def cmd_tree(args: argparse.Namespace) -> int:
    print(
        "pcitopo: `tree` is not built yet. Stage 2 reads the Type 1 bridge headers, "
        "stage 3 assembles the tree from them. `pcitopo list` works now.",
        file=sys.stderr,
    )
    return NOT_YET


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "tree":
        return cmd_tree(args)
    return NOT_YET  # unreachable: argparse rejects any other command first
