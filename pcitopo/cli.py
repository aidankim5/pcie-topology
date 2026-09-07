"""Command line.

Four verbs, which are the four questions the tool answers:

    list    every PCI function, flat, with its identity fields
    buses   every Type 1 header and the bus numbers it carries, checked
    tree    the topology assembled from those bus numbers, annotated
    serve   the same tree in a browser, with a Python process behind it

Every verb reads from one of two sources, and they are interchangeable because
both end as a `Scan`: the live `/sys` (or a captured copy of it, via
--sysfs-root), or a saved `lspci -vvv -xxxx` dump (via --lspci). The second is
what makes the tool usable on a machine that is not the one being inspected.

`list` and `buses` are not scaffolding left over from building `tree`. They are
the two halves of it shown separately, and they are what you reach for when the
tree looks wrong: `list` says what the machine has, `buses` says whether the
numbers that arrange it are self-consistent, and only then does `tree` draw a
picture that depends on both being right.

Exit codes (a choice, not spec, and the same ones the decoder repo uses):
  0  done
  1  the sysfs tree is missing or unreadable
  2  usage error (argparse's own convention: unknown command or flag)
  3  the requested part of the tool is not built yet
"""

import argparse
import sys

from pathlib import Path

from . import __version__
from .export import to_dot, to_json
from .ids import PciIds
from .lspci import NotAnLspciDump, scan_from_lspci
from .model import build_devices
from .render import (
    render_access_note,
    render_bus_findings,
    render_buses,
    render_degraded,
    render_list,
    render_problems,
    render_scan_summary,
    render_topology_warnings,
    render_tree,
)
from .server import serve, write_html
from .sysfs import DEFAULT_SYSFS_ROOT, SysfsUnavailable, scan
from .topology import build_topology, cross_check
from .webui import build_payload

OK, BAD_INPUT, NOT_YET = 0, 1, 3  # 2 is taken by argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pcitopo",
        description="Draw the PCI Express topology of a live Linux machine.",
    )
    p.add_argument("--version", action="version", version=f"pcitopo {__version__}")

    # One sub-command per verb. dest="cmd" stores which one was typed;
    # required=True refuses none.
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add_common(sp: argparse.ArgumentParser) -> None:
        # Mutually exclusive: a run reads one machine, from one source. Letting both
        # through would raise the question of which wins, and there is no good answer.
        source = sp.add_mutually_exclusive_group()
        source.add_argument(
            "--lspci",
            metavar="FILE",
            help=(
                "read a saved `sudo lspci -vvv -xxxx` dump instead of sysfs. The hex rows "
                "in it are the same bytes sysfs would hand over, so everything works the "
                "same way -- on any machine, with no hardware present."
            ),
        )
        source.add_argument(
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

    buses = sub.add_parser(
        "buses",
        help="the Type 1 bridge headers and their bus numbers",
        description=(
            "Every bridge's Primary, Secondary and Subordinate Bus Numbers (18h-1Ah), "
            "with the consistency checks the tree depends on. Run this when a tree "
            "looks wrong: it shows the numbers the tree is built from, unassembled."
        ),
    )
    add_common(buses)

    tree = sub.add_parser(
        "tree",
        help="the topology as an annotated tree",
        description=(
            "Assemble the tree by matching every device's bus number against the "
            "bridges' Secondary Bus Numbers, annotate each node with its port type and "
            "link, and cross-check the result against the kernel's own sysfs nesting."
        ),
    )
    add_common(tree)
    tree.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also show BARs, bridge forwarding windows and the capability chain",
    )
    tree.add_argument(
        "--degraded",
        action="store_true",
        help="instead of the tree, list only links running below their maximum",
    )
    tree.add_argument(
        "--json",
        action="store_true",
        help="emit the topology as JSON on stdout instead of drawing it",
    )
    tree.add_argument(
        "--html",
        metavar="FILE",
        help=(
            "write a self-contained HTML viewer to FILE: the tree, clickable, with every "
            "decoded field per device. One file, no server, opens in any browser."
        ),
    )
    tree.add_argument(
        "--dot",
        metavar="FILE",
        help=(
            "write a Graphviz DOT block diagram to FILE ('-' for stdout). "
            "Render it with: dot -Tsvg FILE -o topo.svg"
        ),
    )
    srv = sub.add_parser(
        "serve",
        help="the viewer in a browser, with live re-scan and drag-and-drop decoding",
        description=(
            "Serve the visual topology viewer from a local HTTP server. Unlike the "
            "standalone --html file, this one can re-scan the hardware on demand and can "
            "decode an lspci dump dropped onto the page, because Python is still running "
            "behind it."
        ),
    )
    add_common(srv)
    srv.add_argument(
        "--port",
        type=int,
        default=8765,
        metavar="N",
        help="port to listen on (default: %(default)s). 0 asks the OS for any free port.",
    )
    srv.add_argument(
        "--host",
        default="127.0.0.1",
        metavar="ADDR",
        help=(
            "address to bind (default: %(default)s, this machine only). Setting anything "
            "else exposes your hardware inventory to the network, and this server has no "
            "authentication of any kind."
        ),
    )
    srv.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser window; just print the URL",
    )
    return p


def load_ids(path: str | None) -> PciIds:
    """The name database: the file the user named, else the usual system locations."""
    return PciIds.load((path,) if path else None)


def _scan_or_fail(args: argparse.Namespace):
    """The read every command starts with. Returns None after printing why it failed.

    Both branches return a `Scan`, so nothing downstream has to know which one ran.
    That is the whole reason lspci.py returns one too.
    """
    if getattr(args, "lspci", None):
        path = Path(args.lspci)
        try:
            # errors="replace" because one bad byte in a large dump should cost that
            # character, not the capture.
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"pcitopo: cannot read {path}: {exc.strerror}", file=sys.stderr)
            return None
        try:
            return scan_from_lspci(text, source=str(path))
        except NotAnLspciDump as exc:
            print(f"pcitopo: {path}: {exc}", file=sys.stderr)
            return None

    try:
        return scan(args.sysfs_root)
    except SysfsUnavailable as exc:
        print(f"pcitopo: {exc}", file=sys.stderr)
        return None


def _print_problems(result, devices) -> None:
    problems = render_problems(result, devices)
    if problems:
        print()
        for line in problems:
            print(line, file=sys.stderr)


def cmd_list(args: argparse.Namespace) -> int:
    result = _scan_or_fail(args)
    if result is None:
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

    _print_problems(result, devices)
    return OK


def cmd_buses(args: argparse.Namespace) -> int:
    result = _scan_or_fail(args)
    if result is None:
        return BAD_INPUT

    ids = load_ids(args.ids)
    devices = build_devices(result)

    for line in render_scan_summary(result, devices, ids):
        print(line)
    print()
    for line in render_buses(devices):
        print(line)
    print()
    for line in render_bus_findings(devices):
        print(line)
    return OK


def cmd_tree(args: argparse.Namespace) -> int:
    result = _scan_or_fail(args)
    if result is None:
        return BAD_INPUT

    ids = load_ids(args.ids)
    devices = build_devices(result)
    topo = build_topology(devices)
    cross_check(topo, devices)

    if args.html:
        payload = build_payload(topo, ids, result, devices, source=str(result.devices_dir))
        write_html(payload, args.html)
        print(f"Wrote {args.html}. Open it in any browser; no server needed.")
        if not args.json and not args.degraded and not args.dot:
            return OK

    # --dot writes a file (or stdout) and is independent of what else is printed, so
    # it runs first and does not suppress the tree unless --json also asked for quiet.
    if args.dot:
        dot = to_dot(topo, ids)
        if args.dot == "-":
            print(dot)
        else:
            with open(args.dot, "w", encoding="utf-8") as fh:
                fh.write(dot + "\n")
            print(f"Wrote {args.dot}. Render it with: dot -Tsvg {args.dot} -o topo.svg")
        if not args.json and not args.degraded:
            return OK

    if args.json:
        print(to_json(topo, ids))
        return OK

    if args.degraded:
        for line in render_degraded(devices, ids):
            print(line)
        return OK

    for line in render_scan_summary(result, devices, ids):
        print(line)
    for line in render_access_note(result):
        print(line)
    print()
    for line in render_tree(topo, ids, verbose=args.verbose):
        print(line)

    warnings = render_topology_warnings(topo)
    if warnings:
        for line in warnings:
            print(line, file=sys.stderr)
    _print_problems(result, devices)
    return OK


def cmd_serve(args: argparse.Namespace) -> int:
    # Fail fast on an unreadable source rather than serving a page that cannot work.
    # An lspci dump is loaded once here and becomes the page's starting topology.
    if getattr(args, "lspci", None):
        result = _scan_or_fail(args)
        if result is None:
            return BAD_INPUT

    if args.host != "127.0.0.1":
        print(
            f"pcitopo: binding {args.host}, which is reachable from the network. "
            "This server has no authentication and reports your hardware in detail.",
            file=sys.stderr,
        )

    try:
        return serve(
            sysfs_root=args.sysfs_root,
            ids=load_ids(args.ids),
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
    except OSError as exc:
        print(f"pcitopo: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return BAD_INPUT


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "buses":
        return cmd_buses(args)
    if args.cmd == "tree":
        return cmd_tree(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    return NOT_YET  # unreachable: argparse rejects any other command first
