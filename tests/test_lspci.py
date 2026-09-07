"""Reading `lspci -vvv -xxxx` back into configuration space.

The strongest test in this file is not a hand-written fixture. The capture in
`tests/fixtures/` holds the same machine recorded two ways in one boot: sysfs
`config` files, and an lspci dump. Parsing the dump has to reproduce the sysfs
bytes. Anything the parser gets wrong shows up as a difference.

One real subtlety that test documents: the two recordings are *not* identical
byte for byte, and should not be expected to be. Configuration space is live
hardware state, not a file. `capture.sh` copies sysfs first and runs lspci
afterwards, so registers that move on their own -- counters, link statistics --
were read seconds apart and hold different values. The configuration header and
every register this tool decodes are stable and do match exactly, which is the
assertion worth making.
"""

import unittest
from pathlib import Path

from pcitopo.lspci import (
    NotAnLspciDump,
    parse_lspci,
    scan_from_lspci,
)
from pcitopo.model import build_devices
from pcitopo.sysfs import scan

FIXTURES = Path(__file__).parent / "fixtures"
DUMP = FIXTURES / "dumps" / "lspci-full-capture.txt"
SYSFS = FIXTURES / "sysfs-capture-raptorlake"

HEADER_END = 0x40

SMALL_DUMP = """\
00:00.0 Host bridge: Intel Corporation Raptor Lake-S Host Bridge/DRAM Controller (rev 01)
\tSubsystem: ASUSTeK Computer Inc. Device 8882
\tControl: I/O- Mem+ BusMaster+
00: 86 80 00 a7 06 00 90 00 01 00 00 06 00 00 00 00
10: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 20
20: 00 00 00 00 00 00 00 00 00 00 00 00 43 10 82 88
30: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00

00:01.0 PCI bridge: Intel Corporation Raptor Lake PCI Express 5.0 Graphics Port (rev 01)
\tBus: primary=00, secondary=01, subordinate=01, sec-latency=0
00: 86 80 0d a7 07 04 10 00 01 00 04 06 00 00 81 00
10: 00 00 00 00 00 00 00 00 00 01 01 00 f1 01 00 00
20: 00 fa 00 fb 00 00 00 00 00 00 00 00 00 00 00 00
30: 00 00 00 00 40 00 00 00 00 00 00 00 ff 01 12 00
"""


class TestParsingShape(unittest.TestCase):
    def test_finds_each_device_block(self):
        devices = parse_lspci(SMALL_DUMP)
        self.assertEqual([str(d.address) for d in devices], ["0000:00:00.0", "0000:00:01.0"])

    def test_assembles_the_hex_rows_in_offset_order(self):
        config = parse_lspci(SMALL_DUMP)[0].config()
        self.assertEqual(len(config), HEADER_END)
        self.assertEqual(config[0x00:0x04], bytes([0x86, 0x80, 0x00, 0xA7]))
        self.assertEqual(config[0x3C], 0x00)

    def test_a_device_line_is_not_mistaken_for_a_hex_row(self):
        # "00:00.0 Host bridge:" starts with hex and a colon, like a row does.
        # The difference is the ".0 " and the missing space after the first colon.
        self.assertEqual(len(parse_lspci(SMALL_DUMP)), 2)

    def test_indented_detail_lines_are_ignored(self):
        # lspci's own decoded lines are its interpretation; this tool makes its own.
        bridge = parse_lspci(SMALL_DUMP)[1]
        self.assertEqual(len(bridge.config()), HEADER_END)

    def test_bus_numbers_survive_the_round_trip(self):
        # 18h-1Ah of the second block: primary 00, secondary 01, subordinate 01.
        config = parse_lspci(SMALL_DUMP)[1].config()
        self.assertEqual((config[0x18], config[0x19], config[0x1A]), (0x00, 0x01, 0x01))

    def test_domain_defaults_to_zero_when_lspci_omits_it(self):
        self.assertEqual(parse_lspci(SMALL_DUMP)[0].address.domain, 0)

    def test_domain_is_read_when_lspci_prints_it(self):
        text = "0001:00:00.0 Host bridge: Intel\n00: 86 80 00 a7\n"
        self.assertEqual(parse_lspci(text)[0].address.domain, 1)

    def test_name_hint_drops_the_class_and_the_revision(self):
        hint = parse_lspci(SMALL_DUMP)[0].name_hint
        self.assertEqual(hint, "Intel Corporation Raptor Lake-S Host Bridge/DRAM Controller")

    def test_scan_produces_the_same_type_a_live_read_does(self):
        result = scan_from_lspci(SMALL_DUMP)
        self.assertEqual(len(result.devices), 2)
        self.assertEqual(result.domains, [0])

    def test_devices_come_back_in_address_order(self):
        text = SMALL_DUMP + "\n00:00.1 Audio: X\n00: 86 80 00 a7\n"
        addresses = [str(d.address) for d in scan_from_lspci(text).devices]
        self.assertEqual(addresses, sorted(addresses))

    def test_no_attribute_files_are_invented(self):
        # A dump has only bytes. Claiming kernel attributes would fake a cross-check.
        self.assertEqual(scan_from_lspci(SMALL_DUMP).devices[0].attrs, {})


class TestParsingFailures(unittest.TestCase):
    def test_text_that_is_not_a_dump_is_rejected(self):
        with self.assertRaises(NotAnLspciDump):
            scan_from_lspci("this is just some notes I wrote\n")

    def test_a_dump_taken_without_x_says_which_flag_is_missing(self):
        text = "00:00.0 Host bridge: Intel Corporation Thing (rev 01)\n\tControl: I/O-\n"
        with self.assertRaises(NotAnLspciDump) as caught:
            scan_from_lspci(text)
        self.assertIn("-xxxx", str(caught.exception))

    def test_an_empty_file_is_rejected(self):
        with self.assertRaises(NotAnLspciDump):
            scan_from_lspci("")

    def test_a_gap_in_the_rows_is_reported_not_hidden(self):
        text = "00:00.0 Host bridge: Intel\n00: 86 80 00 a7\n20: 01 02 03 04\n"
        device = parse_lspci(text)[0]
        config = device.config()
        self.assertEqual(len(config), 0x24)
        self.assertTrue(any("gaps" in p for p in device.problems))
        self.assertEqual(config[0x10], 0)  # zero-filled, and flagged


class TestAgainstTheSysfsCapture(unittest.TestCase):
    """The same machine, recorded twice in one boot, must decode the same way."""

    @classmethod
    def setUpClass(cls):
        cls.from_dump = {
            str(d.address): d for d in scan_from_lspci(DUMP.read_text(errors="replace")).devices
        }
        cls.from_sysfs = {str(d.address): d for d in scan(str(SYSFS)).devices}

    def test_the_same_devices_are_found(self):
        self.assertEqual(set(self.from_dump), set(self.from_sysfs))

    def test_the_same_number_of_bytes_was_captured_for_each(self):
        for address, dev in self.from_sysfs.items():
            with self.subTest(address=address):
                self.assertEqual(len(self.from_dump[address].config), len(dev.config))

    def test_every_configuration_header_is_byte_identical(self):
        # 00h-3Fh is stable state: identity, header type, bus numbers. If the parser
        # dropped, reordered or misaligned a row, this is where it would show.
        for address, dev in self.from_sysfs.items():
            with self.subTest(address=address):
                self.assertEqual(
                    self.from_dump[address].config[:HEADER_END],
                    dev.config[:HEADER_END],
                )

    def test_everything_this_tool_decodes_is_identical(self):
        dump = {str(d.address): d for d in build_devices(scan_from_lspci(DUMP.read_text(errors="replace")))}
        sysfs = {str(d.address): d for d in build_devices(scan(str(SYSFS)))}
        fields = (
            "vendor_id", "device_id", "revision", "class_code", "is_bridge",
            "multi_function", "secondary_bus", "subordinate_bus", "port_type_name",
            "link_text", "degraded",
        )
        for address in sysfs:
            for field in fields:
                with self.subTest(address=address, field=field):
                    self.assertEqual(
                        getattr(dump[address], field), getattr(sysfs[address], field)
                    )

    def test_lspci_names_are_carried_across_as_hints(self):
        self.assertEqual(
            self.from_dump["0000:06:00.0"].name_hint,
            "Intel Corporation Ethernet Controller I226-V",
        )

    def test_a_dump_offers_no_kernel_nesting_to_cross_check(self):
        # There are no symlinks in a text file, and the tool must not pretend otherwise.
        from pcitopo.topology import build_topology, cross_check

        devices = build_devices(scan_from_lspci(DUMP.read_text(errors="replace")))
        topo = build_topology(devices)
        cross_check(topo, devices)
        self.assertFalse(topo.checked_against_sysfs)


if __name__ == "__main__":
    unittest.main()
