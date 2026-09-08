"""The real machine: 27 functions captured from live hardware, checked against lspci.

These are the tests that would catch a decoder that is self-consistent and
wrong. Every assertion here is anchored to something produced independently:
`lspci -nn` for identity and `lspci -tv` for the shape of the tree, both taken
in the same boot as the configuration-space bytes.

The two fixture trees are the same capture at two privilege levels, so the pair
also proves the claim the whole tool is organised around: identity and topology
survive an unprivileged read, and link speed does not.
"""

import json
import re
import unittest
from pathlib import Path

from pcitopo.export import to_dot, to_json
from pcitopo.ids import PciIds
from pcitopo.model import build_devices
from pcitopo.sysfs import scan
from pcitopo.topology import build_topology, degraded_devices

FIXTURES = Path(__file__).parent / "fixtures"
FULL = FIXTURES / "sysfs-capture-raptorlake"
NOROOT = FIXTURES / "sysfs-capture-raptorlake-noroot"
LSPCI_TV = FIXTURES / "dumps" / "lspci-tv-capture.txt"
LSPCI_NN = FIXTURES / "dumps" / "lspci-nn-capture.txt"

DEVICE_COUNT = 27
BRIDGE_COUNT = 7

# `lspci -nn` line: "00:01.0 PCI bridge [0604]: Intel ... [8086:a70d] (rev 01)"
LSPCI_NN_RE = re.compile(
    r"^(?P<bdf>[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]) "
    r".*\[(?P<class>[0-9a-f]{4})\]: "
    r".*\[(?P<vendor>[0-9a-f]{4}):(?P<device>[0-9a-f]{4})\]"
    r"(?: \(rev (?P<rev>[0-9a-f]{2})\))?",
    re.MULTILINE,
)

# `lspci -tv` writes a bridge as "+-01.0-[01]--" or "+-1c.0-[04-07]--".
LSPCI_TV_BRIDGE_RE = re.compile(
    r"([0-9a-f]{2}\.[0-7])-\[([0-9a-f]{2})(?:-([0-9a-f]{2}))?\]"
)

# A DOT node definition, as export.to_dot writes it: a label followed by a shape.
# The label body allows backslash escapes so a \" does not end the match early.
NODE_DEF_RE = re.compile(r'\[label="((?:[^"\\]|\\.)*)", shape=')
LABEL_RE = re.compile(r'\[label="((?:[^"\\]|\\.)*)"')


def load(root: Path):
    devices = build_devices(scan(str(root)))
    return devices, {str(d.address): d for d in devices}


class TestCaptureIdentity(unittest.TestCase):
    """Every identity field, against `lspci -nn` from the same boot."""

    @classmethod
    def setUpClass(cls):
        cls.devices, cls.by_address = load(FULL)
        cls.lspci = {
            m.group("bdf"): m.groupdict() for m in LSPCI_NN_RE.finditer(LSPCI_NN.read_text())
        }

    def test_every_function_was_enumerated(self):
        self.assertEqual(len(self.devices), DEVICE_COUNT)

    def test_lspci_and_the_tool_see_the_same_functions(self):
        ours = {d.address.bdf for d in self.devices}
        self.assertEqual(ours, set(self.lspci))

    def test_vendor_and_device_ids_match_lspci(self):
        for bdf, expected in self.lspci.items():
            dev = self.by_address["0000:" + bdf]
            with self.subTest(bdf=bdf):
                self.assertEqual(dev.vendor_id, int(expected["vendor"], 16))
                self.assertEqual(dev.device_id, int(expected["device"], 16))

    def test_class_codes_match_lspci(self):
        # lspci -nn prints the top 16 bits: base class and sub-class, no prog-if.
        for bdf, expected in self.lspci.items():
            dev = self.by_address["0000:" + bdf]
            with self.subTest(bdf=bdf):
                self.assertEqual(dev.class_code >> 8, int(expected["class"], 16))

    def test_revisions_match_lspci(self):
        for bdf, expected in self.lspci.items():
            if expected["rev"] is None:
                continue
            dev = self.by_address["0000:" + bdf]
            with self.subTest(bdf=bdf):
                self.assertEqual(dev.revision, int(expected["rev"], 16))

    def test_no_device_disagrees_with_the_kernel(self):
        # model.py cross-checks its own decode against the sysfs attribute files.
        # On a real capture the two must agree everywhere.
        for dev in self.devices:
            self.assertEqual(dev.warnings, [], f"{dev.address} reported {dev.warnings}")

    def test_the_capture_was_taken_with_root(self):
        self.assertTrue(all(d.config.size > 64 for d in self.devices))


class TestCaptureTopology(unittest.TestCase):
    """The tree, against `lspci -tv` from the same boot."""

    @classmethod
    def setUpClass(cls):
        cls.devices, cls.by_address = load(FULL)
        cls.topo = build_topology(cls.devices)
        cls.tv = LSPCI_TV.read_text()

    def test_seven_bridges(self):
        self.assertEqual(sum(1 for d in self.devices if d.is_bridge), BRIDGE_COUNT)

    def test_one_domain(self):
        self.assertEqual([t.domain for t in self.topo.domains], [0])

    def test_secondary_bus_numbers_match_lspci_tv(self):
        expected = {
            df: int(sec, 16) for df, sec, _sub in LSPCI_TV_BRIDGE_RE.findall(self.tv)
        }
        ours = {
            d.address.device_function: d.secondary_bus for d in self.devices if d.is_bridge
        }
        self.assertEqual(ours, expected)

    def test_subordinate_bus_numbers_match_lspci_tv(self):
        for df, sec, sub in LSPCI_TV_BRIDGE_RE.findall(self.tv):
            expected = int(sub, 16) if sub else int(sec, 16)
            dev = next(d for d in self.devices if d.address.device_function == df and d.is_bridge)
            with self.subTest(bridge=df):
                self.assertEqual(dev.subordinate_bus, expected)

    def test_every_device_is_in_the_tree_exactly_once(self):
        addresses = [str(n.address) for n in self.topo.walk()]
        self.assertEqual(len(addresses), DEVICE_COUNT)
        self.assertEqual(len(set(addresses)), DEVICE_COUNT)

    def test_no_orphans_on_real_hardware(self):
        self.assertEqual(self.topo.domains[0].orphans, [])

    def test_every_child_sits_on_its_parents_secondary_bus(self):
        for node in self.topo.walk():
            for child in node.children:
                with self.subTest(parent=str(node.address), child=str(child.address)):
                    self.assertEqual(child.address.bus, node.device.secondary_bus)

    def test_the_gpu_hangs_off_the_graphics_port(self):
        port = next(n for n in self.topo.walk() if str(n.address) == "0000:00:01.0")
        self.assertEqual(
            sorted(str(c.address) for c in port.children),
            ["0000:01:00.0", "0000:01:00.1"],
        )

    def test_the_gpu_audio_function_is_a_sibling_not_a_child(self):
        gpu = next(n for n in self.topo.walk() if str(n.address) == "0000:01:00.0")
        self.assertEqual(gpu.children, [])

    def test_empty_slots_have_a_bus_number_and_no_children(self):
        for address in ("0000:00:1b.0", "0000:00:1d.0"):
            node = next(n for n in self.topo.walk() if str(n.address) == address)
            with self.subTest(address=address):
                self.assertIsNotNone(node.device.secondary_bus)
                self.assertEqual(node.children, [])

    def test_root_bus_holds_every_bridge(self):
        # This machine has no switches: every Type 1 header is a Root Port on bus 0.
        self.assertTrue(all(d.address.bus == 0 for d in self.devices if d.is_bridge))


class TestCaptureLinks(unittest.TestCase):
    """Link speed and width, which only exist because the capture was taken as root."""

    @classmethod
    def setUpClass(cls):
        cls.devices, cls.by_address = load(FULL)
        cls.topo = build_topology(cls.devices)

    def test_root_ports_decode_as_root_ports(self):
        for dev in self.devices:
            if dev.is_bridge:
                with self.subTest(address=str(dev.address)):
                    self.assertEqual(dev.port_type_name, "Root Port")

    def test_the_nvme_link_is_at_full_rate(self):
        ssd = self.by_address["0000:02:00.0"]
        self.assertEqual(ssd.pcie.current.width, 4)
        self.assertEqual(ssd.link_text, "16 GT/s x4 (max 16 GT/s x4)")
        self.assertFalse(ssd.degraded)

    def test_the_idle_gpu_is_parked_at_2_5_gt_s(self):
        gpu = self.by_address["0000:01:00.0"]
        self.assertEqual(gpu.pcie.current.speed, 1)
        self.assertEqual(gpu.pcie.maximum.speed, 4)
        self.assertEqual(gpu.pcie.current.width, 16)
        self.assertTrue(gpu.degraded)
        self.assertIn("2.5 GT/s of 16 GT/s", gpu.pcie.degraded_reason)

    def test_the_graphics_port_is_gen5_even_though_the_card_is_gen4(self):
        # The port supports 32 GT/s, the card 16. The link is the slower of the two.
        port = self.by_address["0000:00:01.0"]
        self.assertEqual(port.pcie.maximum.speed, 5)
        self.assertEqual(self.by_address["0000:01:00.0"].pcie.maximum.speed, 4)

    def test_empty_slots_report_no_link(self):
        for address in ("0000:00:1b.0", "0000:00:1d.0"):
            dev = self.by_address[address]
            with self.subTest(address=address):
                self.assertEqual(dev.pcie.current.width, 0)
                self.assertFalse(dev.pcie.link_up)
                self.assertFalse(dev.degraded)

    def test_integrated_endpoints_have_no_link_registers(self):
        for address in ("0000:00:0a.0", "0000:00:0e.0"):
            dev = self.by_address[address]
            with self.subTest(address=address):
                self.assertEqual(dev.port_type_name, "Root Complex Integrated Endpoint")
                self.assertFalse(dev.pcie.has_link)

    def test_degraded_list_holds_only_links_that_are_up(self):
        for dev in degraded_devices(self.topo):
            with self.subTest(address=str(dev.address)):
                self.assertGreater(dev.pcie.current.width, 0)

    def test_the_gpu_appears_in_the_degraded_list(self):
        addresses = [str(d.address) for d in degraded_devices(self.topo)]
        self.assertIn("0000:01:00.0", addresses)

    def test_extended_capabilities_are_reachable_in_the_4096_byte_reads(self):
        gpu = self.by_address["0000:01:00.0"]
        self.assertEqual(gpu.config.size, 4096)
        self.assertTrue(gpu.ext_caps)

    def test_the_gpu_has_bars(self):
        gpu = self.by_address["0000:01:00.0"]
        implemented = [b for b in gpu.bars if b.implemented]
        self.assertTrue(implemented)
        self.assertTrue(any(b.is_64bit for b in implemented))

    def test_bridges_carry_forwarding_windows(self):
        port = self.by_address["0000:00:01.0"]
        kinds = {w.kind for w in port.windows}
        self.assertEqual(kinds, {"I/O", "memory", "prefetchable memory"})


class TestUnprivilegedCapture(unittest.TestCase):
    """The same machine read as a normal user: 64 bytes per device, nothing more."""

    @classmethod
    def setUpClass(cls):
        cls.full, cls.full_by = load(FULL)
        cls.limited, cls.limited_by = load(NOROOT)

    def test_every_read_stops_at_64_bytes(self):
        self.assertTrue(all(d.config.size == 64 for d in self.limited))

    def test_the_same_devices_are_found(self):
        self.assertEqual(len(self.limited), DEVICE_COUNT)

    def test_identity_is_unchanged(self):
        for address, dev in self.limited_by.items():
            with self.subTest(address=address):
                self.assertEqual(dev.vendor_id, self.full_by[address].vendor_id)
                self.assertEqual(dev.device_id, self.full_by[address].device_id)
                self.assertEqual(dev.class_code, self.full_by[address].class_code)

    def test_bus_numbers_are_unchanged(self):
        for address, dev in self.limited_by.items():
            with self.subTest(address=address):
                self.assertEqual(dev.secondary_bus, self.full_by[address].secondary_bus)
                self.assertEqual(dev.subordinate_bus, self.full_by[address].subordinate_bus)

    def test_the_tree_is_identical(self):
        def shape(devices):
            topo = build_topology(devices)
            return sorted(
                (str(n.address), sorted(str(c.address) for c in n.children))
                for n in topo.walk()
            )

        self.assertEqual(shape(self.limited), shape(self.full))

    def test_no_link_information_survives(self):
        self.assertTrue(all(d.pcie is None for d in self.limited))
        self.assertTrue(all(d.link_text == "" for d in self.limited))

    def test_no_degraded_link_can_be_reported(self):
        self.assertEqual(degraded_devices(build_topology(self.limited)), [])

    def test_the_capability_chain_is_out_of_reach(self):
        self.assertTrue(all(d.caps == [] for d in self.limited))


class TestExports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.devices, cls.by_address = load(FULL)
        cls.topo = build_topology(cls.devices)
        cls.ids = PciIds.parse("")

    def test_json_is_valid_and_holds_every_device(self):
        doc = json.loads(to_json(self.topo, self.ids))

        def count(nodes):
            return sum(1 + count(n["children"]) for n in nodes)

        self.assertEqual(count(doc["domains"][0]["devices"]), DEVICE_COUNT)

    def test_json_carries_the_link_pair(self):
        doc = json.loads(to_json(self.topo, self.ids))
        port = next(d for d in doc["domains"][0]["devices"] if d["address"] == "0000:00:01.0")
        self.assertEqual(port["link"]["max_speed_encoding"], 5)
        self.assertEqual(port["link"]["current_speed_encoding"], 1)
        self.assertTrue(port["link"]["degraded"])

    def test_json_nests_children_under_their_parent(self):
        doc = json.loads(to_json(self.topo, self.ids))
        port = next(d for d in doc["domains"][0]["devices"] if d["address"] == "0000:00:01.0")
        self.assertEqual(
            sorted(c["address"] for c in port["children"]),
            ["0000:01:00.0", "0000:01:00.1"],
        )

    def test_dot_is_a_digraph_with_one_node_per_device(self):
        dot = to_dot(self.topo, self.ids)
        self.assertTrue(dot.startswith("digraph pcie {"))
        # Node definitions carry a shape; edges also have labels, so counting
        # "[label=" alone would count the edges too.
        nodes = NODE_DEF_RE.findall(dot)
        self.assertEqual(len(nodes), DEVICE_COUNT + 1)  # +1 for the root complex

    def test_dot_draws_an_edge_for_every_parent_child_pair(self):
        dot = to_dot(self.topo, self.ids)
        self.assertIn("dev_0000_00_01_0 -> dev_0000_01_00_0", dot)

    def test_dot_labels_contain_no_unescaped_quote(self):
        # A raw quote inside a label would end the string early and produce a file
        # Graphviz cannot parse, so the escaping is worth pinning down.
        dot = to_dot(self.topo, self.ids)
        labels = LABEL_RE.findall(dot)
        self.assertTrue(labels)
        for label in labels:
            # Strip the backslash escapes first; any quote left over is a bare one,
            # which would end the DOT string early.
            self.assertNotIn('"', re.sub(r"\\.", "", label))


if __name__ == "__main__":
    unittest.main()
