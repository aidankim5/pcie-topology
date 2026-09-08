"""Tree building: the rule, the edge cases, and the cross-check against sysfs.

Devices here are built from bytes rather than from a fixture directory, so a
test can describe a machine that does not exist -- two domains, a bridge with a
backwards range, a device on an unclaimed bus -- and see what the tree does
with it. Those cases are the reason the checks exist and cannot be captured
from working hardware.
"""

import unittest
from pathlib import Path

from pcitopo.model import build_device
from pcitopo.render import render_bus_findings, render_buses, render_tree
from pcitopo.ids import PciIds
from pcitopo.sysfs import Address, SysfsDevice
from pcitopo.topology import build_topology, cross_check, degraded_devices


def config_bytes(
    *,
    bridge: bool = False,
    vendor: int = 0x8086,
    device: int = 0x1234,
    class_code: int = 0x060400,
    primary: int = 0,
    secondary: int = 0,
    subordinate: int = 0,
    pcie_port_type: int | None = None,
    max_speed: int = 4,
    max_width: int = 16,
    cur_speed: int = 4,
    cur_width: int = 16,
    size: int = 256,
) -> bytes:
    """One function's configuration space, built to order."""
    data = bytearray(size)
    data[0x00:0x02] = vendor.to_bytes(2, "little")
    data[0x02:0x04] = device.to_bytes(2, "little")
    data[0x08] = 0x01
    data[0x09] = class_code & 0xFF
    data[0x0A] = (class_code >> 8) & 0xFF
    data[0x0B] = (class_code >> 16) & 0xFF
    data[0x0E] = 0x01 if bridge else 0x00

    if bridge:
        data[0x18], data[0x19], data[0x1A] = primary, secondary, subordinate

    if pcie_port_type is not None and size > 0x80:
        data[0x06:0x08] = (0x0010).to_bytes(2, "little")  # Status bit 4
        data[0x34] = 0x80
        cap = 0x80
        data[cap], data[cap + 1] = 0x10, 0x00
        data[cap + 2 : cap + 4] = ((pcie_port_type << 4) | 2).to_bytes(2, "little")
        data[cap + 0x0C : cap + 0x10] = ((max_width << 4) | max_speed).to_bytes(4, "little")
        data[cap + 0x12 : cap + 0x14] = ((cur_width << 4) | cur_speed).to_bytes(2, "little")

    return bytes(data)


def make_device(address: str, *, real_path: str | None = None, **kwargs):
    """One Device, as model.py would build it from a sysfs read."""
    addr = Address.parse(address)
    path = Path("/sys/bus/pci/devices") / address
    resolved = Path(real_path) if real_path else path
    return build_device(
        SysfsDevice(
            address=addr,
            path=path,
            real_path=resolved,
            attrs={},
            config=config_bytes(**kwargs),
        )
    )


class TestTreeBuilding(unittest.TestCase):
    def simple_machine(self):
        """Bus 0 with a host bridge and a root port; one endpoint on bus 1."""
        return [
            make_device("0000:00:00.0", class_code=0x060000),
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:01:00.0", class_code=0x030000),
        ]

    def test_root_bus_devices_hang_off_the_domain(self):
        topo = build_topology(self.simple_machine())
        self.assertEqual([str(n.address) for n in topo.domains[0].roots],
                         ["0000:00:00.0", "0000:00:01.0"])

    def test_device_attaches_to_the_bridge_whose_secondary_matches_its_bus(self):
        topo = build_topology(self.simple_machine())
        port = topo.domains[0].roots[1]
        self.assertEqual([str(c.address) for c in port.children], ["0000:01:00.0"])

    def test_the_host_bridge_is_a_peer_not_a_parent(self):
        # 00:00.0 sits on bus 0 like everything else there. Treating it as the root
        # of the tree is a common mistake and would nest every device under it.
        topo = build_topology(self.simple_machine())
        host = topo.domains[0].roots[0]
        self.assertEqual(host.children, [])

    def test_functions_of_one_device_are_siblings(self):
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:01:00.0"),
            make_device("0000:01:00.1"),
        ]
        topo = build_topology(devices)
        port = topo.domains[0].roots[0]
        self.assertEqual(len(port.children), 2)
        self.assertEqual(port.children[0].children, [])

    def test_nested_bridges_build_a_deeper_tree(self):
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=3),
            make_device("0000:01:00.0", bridge=True, primary=1, secondary=2, subordinate=3),
            make_device("0000:02:00.0"),
        ]
        topo = build_topology(devices)
        upstream = topo.domains[0].roots[0]
        switch = upstream.children[0]
        self.assertEqual(str(switch.address), "0000:01:00.0")
        self.assertEqual(str(switch.children[0].address), "0000:02:00.0")

    def test_walk_visits_every_node_once(self):
        topo = build_topology(self.simple_machine())
        self.assertEqual(topo.device_count, 3)

    def test_empty_bridge_has_no_children(self):
        devices = [
            make_device("0000:00:1b.0", bridge=True, primary=0, secondary=4, subordinate=4),
        ]
        topo = build_topology(devices)
        self.assertEqual(topo.domains[0].roots[0].children, [])


class TestDomainsAreIndependent(unittest.TestCase):
    def two_domains(self):
        return [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:01:00.0"),
            make_device("0001:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0001:01:00.0"),
        ]

    def test_each_domain_gets_its_own_tree(self):
        topo = build_topology(self.two_domains())
        self.assertEqual([t.domain for t in topo.domains], [0, 1])

    def test_a_bus_number_does_not_cross_domains(self):
        # Both domains have a bus 1. Each endpoint must attach inside its own.
        topo = build_topology(self.two_domains())
        for tree in topo.domains:
            child = tree.roots[0].children[0]
            self.assertEqual(child.address.domain, tree.domain)

    def test_a_domain_that_does_not_start_at_bus_zero(self):
        devices = [
            make_device("0001:e0:00.0", bridge=True, primary=0xE0, secondary=0xE1, subordinate=0xE1),
            make_device("0001:e1:00.0"),
        ]
        topo = build_topology(devices)
        self.assertEqual(topo.domains[0].root_bus, 0xE0)
        self.assertEqual(len(topo.domains[0].roots), 1)


class TestOrphans(unittest.TestCase):
    def orphaned(self):
        return [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:07:00.0"),  # bus 7: nothing forwards to it
        ]

    def test_orphan_is_kept_not_dropped(self):
        topo = build_topology(self.orphaned())
        self.assertEqual([str(n.address) for n in topo.domains[0].orphans], ["0000:07:00.0"])

    def test_orphan_is_reported(self):
        topo = build_topology(self.orphaned())
        self.assertTrue(any("no bridge names" in w for w in topo.warnings))

    def test_orphan_still_appears_in_the_walk(self):
        topo = build_topology(self.orphaned())
        self.assertEqual(topo.device_count, 2)


class TestCrossCheck(unittest.TestCase):
    def test_agreeing_sources_produce_no_mismatch(self):
        devices = [
            make_device(
                "0000:00:01.0",
                bridge=True,
                primary=0,
                secondary=1,
                subordinate=1,
                real_path="/sys/devices/pci0000:00/0000:00:01.0",
            ),
            make_device(
                "0000:01:00.0",
                real_path="/sys/devices/pci0000:00/0000:00:01.0/0000:01:00.0",
            ),
        ]
        topo = build_topology(devices)
        cross_check(topo, devices)
        self.assertTrue(topo.checked_against_sysfs)
        self.assertEqual([w for w in topo.warnings if "MISMATCH" in w], [])

    def test_disagreeing_sources_are_reported_not_resolved(self):
        # The bus numbers say 01:00.0 is under 00:01.0; sysfs says it is under 00:06.0.
        devices = [
            make_device(
                "0000:00:01.0",
                bridge=True,
                primary=0,
                secondary=1,
                subordinate=1,
                real_path="/sys/devices/pci0000:00/0000:00:01.0",
            ),
            make_device(
                "0000:00:06.0",
                bridge=True,
                primary=0,
                secondary=2,
                subordinate=2,
                real_path="/sys/devices/pci0000:00/0000:00:06.0",
            ),
            make_device(
                "0000:01:00.0",
                real_path="/sys/devices/pci0000:00/0000:00:06.0/0000:01:00.0",
            ),
        ]
        topo = build_topology(devices)
        cross_check(topo, devices)
        self.assertTrue(any("MISMATCH" in w and "0000:01:00.0" in w for w in topo.warnings))

    def test_a_capture_without_symlinks_says_so_instead_of_passing(self):
        devices = [make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1)]
        topo = build_topology(devices)
        cross_check(topo, devices)
        self.assertFalse(topo.checked_against_sysfs)
        self.assertTrue(any("could not be cross-checked" in w for w in topo.warnings))


class TestBusFindings(unittest.TestCase):
    def findings(self, devices):
        return "\n".join(render_bus_findings(devices))

    def test_clean_machine_reports_all_checks_pass(self):
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:01:00.0"),
        ]
        self.assertIn("All checks pass", self.findings(devices))

    def test_backwards_range_is_broken(self):
        devices = [make_device("0000:00:01.0", bridge=True, primary=0, secondary=5, subordinate=2)]
        self.assertIn("BROKEN", self.findings(devices))

    def test_all_zero_bus_numbers_are_unconfigured(self):
        devices = [make_device("0000:00:01.0", bridge=True)]
        self.assertIn("UNCONFIGURED", self.findings(devices))

    def test_wrong_primary_bus_is_a_note_not_an_error(self):
        # PCIe hardware does not route with Primary Bus Number, so a stale value is
        # worth saying out loud but is not a defect.
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=9, secondary=1, subordinate=1),
            make_device("0000:01:00.0"),
        ]
        text = self.findings(devices)
        self.assertIn("NOTE", text)
        self.assertNotIn("BROKEN", text)

    def test_two_bridges_claiming_one_bus_conflict(self):
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:00:06.0", bridge=True, primary=0, secondary=1, subordinate=1),
        ]
        self.assertIn("CONFLICT", self.findings(devices))

    def test_unclaimed_populated_bus_is_an_orphan(self):
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:07:00.0"),
        ]
        self.assertIn("ORPHAN", self.findings(devices))

    def test_child_range_escaping_its_parent_is_broken(self):
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:01:00.0", bridge=True, primary=1, secondary=2, subordinate=9),
        ]
        self.assertIn("BROKEN", self.findings(devices))

    def test_bus_table_marks_an_empty_secondary_bus(self):
        devices = [make_device("0000:00:1b.0", bridge=True, primary=0, secondary=4, subordinate=4)]
        self.assertIn("nothing", "\n".join(render_buses(devices)))


class TestDegraded(unittest.TestCase):
    def test_link_below_maximum_is_degraded(self):
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1,
                        pcie_port_type=4, max_speed=4, cur_speed=1),
        ]
        topo = build_topology(devices)
        self.assertEqual([str(d.address) for d in degraded_devices(topo)], ["0000:00:01.0"])

    def test_link_at_maximum_is_not_degraded(self):
        devices = [make_device("0000:01:00.0", pcie_port_type=0)]
        topo = build_topology(devices)
        self.assertEqual(degraded_devices(topo), [])

    def test_empty_slot_is_not_reported_as_degraded(self):
        devices = [
            make_device("0000:00:1b.0", bridge=True, primary=0, secondary=4, subordinate=4,
                        pcie_port_type=4, cur_width=0, cur_speed=1),
        ]
        topo = build_topology(devices)
        self.assertEqual(degraded_devices(topo), [])


class TestTreeRendering(unittest.TestCase):
    def test_tree_draws_children_indented_under_their_parent(self):
        devices = [
            make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=1),
            make_device("0000:01:00.0"),
        ]
        lines = render_tree(build_topology(devices), PciIds.parse(""))
        child = next(line for line in lines if "0000:01:00.0" in line)
        parent = next(line for line in lines if "0000:00:01.0" in line)
        self.assertGreater(len(child) - len(child.lstrip()), len(parent) - len(parent.lstrip()))

    def test_bridge_headline_carries_its_bus_range(self):
        devices = [make_device("0000:00:01.0", bridge=True, primary=0, secondary=1, subordinate=3)]
        lines = render_tree(build_topology(devices), PciIds.parse(""))
        self.assertIn("[01-03]", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
