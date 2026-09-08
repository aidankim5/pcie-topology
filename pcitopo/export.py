"""Machine-readable output: JSON, and DOT for a real block diagram.

Both are renderers over the same model objects the text output uses, which is
why this module is short. Nothing here decodes anything.

DOT rather than SVG, on purpose. Graphviz's DOT is a text format, so emitting
it needs no dependency at all -- the whole point of the pure-stdlib rule. It
also hands the layout problem to a tool that is good at it: writing SVG
directly would mean solving node placement and edge routing by hand, badly.
Anyone who wants a picture runs

    python3 -m pcitopo tree --dot topo.dot && dot -Tsvg topo.dot -o topo.svg

and anyone who does not still has a file that reads fine as text.

The JSON shape mirrors the tree: each node has its identity, its link, and a
`children` list. A consumer can walk it the same way render.py walks Node.
"""

import json

from .ids import PciIds
from .model import Device
from .topology import Node, Topology


def device_dict(dev: Device, ids: PciIds) -> dict:
    """One device as plain JSON types. None wherever something was not readable.

    Hex numbers are emitted as JSON numbers, not "0x10de" strings: a consumer
    that wants to compare IDs should not have to parse them back. The formatted
    address string is included alongside because that is what a human greps for.
    """
    vendor_id, device_id = dev.vendor_id, dev.device_id
    out = {
        "address": str(dev.address),
        "domain": dev.address.domain,
        "bus": dev.address.bus,
        "device": dev.address.device,
        "function": dev.address.function,
        "vendor_id": vendor_id,
        "device_id": device_id,
        "vendor_name": ids.vendor_name(vendor_id) if vendor_id is not None else None,
        "device_name": ids.device_name(vendor_id, device_id)
        if vendor_id is not None and device_id is not None
        else None,
        "revision": dev.revision,
        "class_code": dev.class_code,
        "class_name": None,
        "header_layout": dev.header.header_layout if dev.header else None,
        "multi_function": dev.multi_function,
        "is_bridge": dev.is_bridge,
        "port_type": dev.pcie.port_type if dev.pcie else None,
        "port_type_name": dev.port_type_name,
        "config_bytes_read": dev.config.size,
        "warnings": list(dev.warnings),
        "problems": list(dev.problems),
    }

    triple = dev.class_triple
    if triple is not None:
        out["class_name"] = ids.class_text(*triple)

    if dev.subsystem is not None:
        out["subsystem_vendor_id"], out["subsystem_device_id"] = dev.subsystem

    if dev.bridge is not None:
        out["buses"] = {
            "primary": dev.bridge.primary_bus,
            "secondary": dev.bridge.secondary_bus,
            "subordinate": dev.bridge.subordinate_bus,
        }

    if dev.pcie is not None and dev.pcie.has_link:
        out["link"] = {
            "current_speed_encoding": dev.pcie.current.speed,
            "current_width": dev.pcie.current.width,
            "max_speed_encoding": dev.pcie.maximum.speed,
            "max_width": dev.pcie.maximum.width,
            "text": dev.pcie.link_text,
            "degraded": dev.pcie.degraded,
            "degraded_reason": dev.pcie.degraded_reason or None,
            "link_active": dev.pcie.link_active,
        }

    if dev.bars:
        out["bars"] = [
            {
                "index": b.index,
                "offset": b.offset,
                "space": b.space,
                "is_64bit": b.is_64bit,
                "prefetchable": b.prefetchable,
                "base": b.base,
            }
            for b in dev.bars
            if b.implemented
        ]

    if dev.windows:
        out["windows"] = [
            {
                "kind": w.kind,
                "enabled": w.enabled,
                "base": w.base if w.enabled else None,
                "limit": w.limit if w.enabled else None,
                "is_64bit": w.is_64bit,
            }
            for w in dev.windows
        ]

    if dev.caps:
        out["capabilities"] = [{"id": c.cap_id, "name": c.name, "offset": c.offset} for c in dev.caps]
    if dev.ext_caps:
        out["extended_capabilities"] = [
            {"id": c.cap_id, "name": c.name, "offset": c.offset, "version": c.version}
            for c in dev.ext_caps
        ]

    return out


def node_dict(node: Node, ids: PciIds) -> dict:
    """One node and its subtree. Recursive, mirroring the tree it came from."""
    out = device_dict(node.device, ids)
    out["children"] = [node_dict(c, ids) for c in node.children]
    return out


def to_json(topo: Topology, ids: PciIds, indent: int = 2) -> str:
    """The whole topology as one JSON document."""
    doc = {
        "domains": [
            {
                "domain": tree.domain,
                "root_bus": tree.root_bus,
                "devices": [node_dict(n, ids) for n in tree.roots],
                "orphans": [node_dict(n, ids) for n in tree.orphans],
            }
            for tree in topo.domains
        ],
        "warnings": list(topo.warnings),
        "cross_checked_against_sysfs": topo.checked_against_sysfs,
    }
    return json.dumps(doc, indent=indent)


# --- DOT ---

# Choice, not spec: how each kind of node is drawn. Shape carries the role so the
# picture is readable without reading every label.
NODE_SHAPES = {
    "Root Port": ("box", "#d8e6f3"),
    "Downstream Port": ("box", "#d8e6f3"),
    "Upstream Port": ("box", "#e6dff3"),
    "Endpoint": ("ellipse", "#e8f3d8"),
    "Legacy Endpoint": ("ellipse", "#e8f3d8"),
    "Root Complex Integrated Endpoint": ("ellipse", "#f3f0d8"),
}
DEFAULT_SHAPE = ("ellipse", "#eeeeee")
DEGRADED_COLOR = "#f3d8d8"


def _dot_escape(text: str) -> str:
    """Quote what DOT treats specially inside a label.

    Backslash first: escaping it after the others would double-escape the
    backslashes those others just inserted.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _dot_id(dev: Device) -> str:
    """A node name DOT will accept: its address with the punctuation removed."""
    a = dev.address
    return f"dev_{a.domain:04x}_{a.bus:02x}_{a.device:02x}_{a.function}"


def _dot_label(dev: Device, ids: PciIds) -> str:
    """The text inside a node: address, name, class, link. One fact per line."""
    vendor_id, device_id = dev.vendor_id, dev.device_id
    name = (
        ids.full_name(vendor_id, device_id)
        if vendor_id is not None and device_id is not None
        else "identity unreadable"
    )
    lines = [str(dev.address), name]

    triple = dev.class_triple
    if triple is not None:
        lines.append(ids.class_text(*triple))
    if dev.bridge and not dev.bridge.unconfigured:
        lines.append(f"buses {dev.bridge.range_text}")
    if dev.link_text:
        lines.append(dev.link_text)
    if dev.degraded:
        lines.append("DEGRADED")

    # \\l is DOT's left-justified line break; \\n would centre every line.
    return "".join(_dot_escape(line) + "\\l" for line in lines)


def to_dot(topo: Topology, ids: PciIds) -> str:
    """The topology as a Graphviz DOT digraph, one cluster per domain."""
    out = [
        "digraph pcie {",
        "  rankdir=LR;",  # left to right: a PCIe tree is wide and shallow
        '  node [shape=box, style="filled,rounded", fontname="Helvetica", fontsize=10];',
        '  edge [color="#666666"];',
        "",
    ]

    for tree in topo.domains:
        root_id = f"domain_{tree.domain:04x}"
        # A cluster_ prefix is what makes Graphviz draw the box around a subgraph.
        out.append(f"  subgraph cluster_{tree.domain:04x} {{")
        out.append(f'    label="Domain {tree.domain:04x}";')
        out.append('    style="rounded"; color="#999999"; fontname="Helvetica";')
        out.append(
            f'    {root_id} [label="Root Complex\\ndomain {tree.domain:04x}", '
            'shape=doubleoctagon, fillcolor="#cccccc"];'
        )

        for node in list(tree.walk()):
            dev = node.device
            shape, fill = NODE_SHAPES.get(dev.port_type_name, DEFAULT_SHAPE)
            if dev.degraded:
                fill = DEGRADED_COLOR
            out.append(
                f'    {_dot_id(dev)} [label="{_dot_label(dev, ids)}", '
                f'shape={shape}, fillcolor="{fill}"];'
            )

        for node in tree.roots + tree.orphans:
            style = ' [style=dashed, label="orphan"]' if node in tree.orphans else ""
            out.append(f"    {root_id} -> {_dot_id(node.device)}{style};")

        for node in tree.walk():
            for child in node.children:
                # The edge label is the link the child actually trained, which is the
                # property of the wire between them rather than of either endpoint.
                label = f' [label="{_dot_escape(child.device.link_text)}"]' if child.device.link_text else ""
                out.append(f"    {_dot_id(node.device)} -> {_dot_id(child.device)}{label};")

        out.append("  }")

    out.append("}")
    return "\n".join(out)
