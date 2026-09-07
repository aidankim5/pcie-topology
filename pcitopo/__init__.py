"""pcitopo: draw the PCI Express topology of a live Linux machine.

Where the data comes from: /sys/bus/pci/devices/ (Linux only). The kernel
publishes one directory per PCI function there, named with its address, and
inside each one the raw configuration space plus a handful of plain-text
attribute files.

What it does with it: reads the Type 1 (bridge) headers, matches each device's
bus number against the bridges' Secondary Bus Numbers, and prints the resulting
tree with an annotation on every node.

This repo is standalone. Some configuration-space helpers are copied from
pcie-config-decoder (the `pcicfg` tool) rather than imported, so neither repo
depends on the other; each copied module says so in its docstring.
"""

__version__ = "0.1.0"
