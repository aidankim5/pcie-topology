"""The byte-level decoders: config space, headers, bridges, BARs, capabilities.

Every test here builds its bytes in the test itself, so what is being asserted
is visible next to the assertion. Tests that read real hardware bytes live in
test_capture.py.
"""

import unittest

from pcitopo.bars import Window, decode_bars, decode_windows
from pcitopo.bridges import Type1Header, decode_type1_header
from pcitopo.capabilities import (
    Capability,
    LinkState,
    PcieCapability,
    decode_pcie_capability,
    find_pcie_capability,
    speed_text,
    walk_capabilities,
    walk_extended_capabilities,
    width_text,
)
from pcitopo.config_space import ConfigSpace
from pcitopo.header import (
    TYPE0,
    TYPE1,
    decode_common_header,
    decode_header_type,
    subsystem_ids,
)


def space(pairs: dict[int, int], size: int = 256) -> ConfigSpace:
    """A ConfigSpace of `size` zero bytes with {offset: byte} written into it."""
    data = bytearray(size)
    for offset, value in pairs.items():
        data[offset] = value
    return ConfigSpace(data=bytes(data))


def write32(data: bytearray, offset: int, value: int) -> None:
    data[offset : offset + 4] = value.to_bytes(4, "little")


class TestConfigSpace(unittest.TestCase):
    def test_u8_reads_one_byte(self):
        self.assertEqual(space({0x0E: 0x81}).u8(0x0E), 0x81)

    def test_u16_is_little_endian(self):
        # Bytes de 10 at offset 0 are Vendor ID 10DEh, NVIDIA.
        cs = ConfigSpace(data=bytes([0xDE, 0x10, 0x89, 0x24]))
        self.assertEqual(cs.u16(0x00), 0x10DE)
        self.assertEqual(cs.u16(0x02), 0x2489)

    def test_u32_is_little_endian(self):
        cs = ConfigSpace(data=bytes([0x04, 0x3D, 0x45, 0x00]))
        self.assertEqual(cs.u32(0x00), 0x00453D04)

    def test_covers_is_exclusive_at_the_end(self):
        cs = ConfigSpace(data=bytes(64))
        self.assertTrue(cs.covers(60, 4))
        self.assertFalse(cs.covers(61, 4))
        self.assertFalse(cs.covers(64, 1))

    def test_reading_past_the_end_raises(self):
        cs = ConfigSpace(data=bytes(64))
        with self.assertRaises(IndexError):
            cs.u32(62)

    def test_header_only_matches_the_unprivileged_read(self):
        self.assertTrue(ConfigSpace(data=bytes(64)).header_only)
        self.assertFalse(ConfigSpace(data=bytes(256)).header_only)

    def test_has_extended_space_needs_more_than_256(self):
        self.assertFalse(ConfigSpace(data=bytes(256)).has_extended_space)
        self.assertTrue(ConfigSpace(data=bytes(4096)).has_extended_space)

    def test_frame_names_the_three_sizes(self):
        self.assertIn("header only", ConfigSpace(data=bytes(64)).frame)
        self.assertIn("256", ConfigSpace(data=bytes(256)).frame)
        self.assertIn("4096", ConfigSpace(data=bytes(4096)).frame)


class TestHeaderType(unittest.TestCase):
    def test_type0_single_function(self):
        self.assertEqual(decode_header_type(0x00), (TYPE0, False))

    def test_type0_multifunction_sets_bit7(self):
        self.assertEqual(decode_header_type(0x80), (TYPE0, True))

    def test_type1_bridge(self):
        self.assertEqual(decode_header_type(0x01), (TYPE1, False))

    def test_type1_multifunction(self):
        self.assertEqual(decode_header_type(0x81), (TYPE1, True))

    def test_multifunction_bit_does_not_change_the_layout(self):
        # 81h is a bridge, not "layout 0x81". Masking bit 7 off is the whole point.
        layout, multi = decode_header_type(0x81)
        self.assertEqual(layout, TYPE1)
        self.assertTrue(multi)


class TestCommonHeader(unittest.TestCase):
    def build(self) -> ConfigSpace:
        data = bytearray(256)
        data[0x00:0x02] = (0x10DE).to_bytes(2, "little")
        data[0x02:0x04] = (0x2489).to_bytes(2, "little")
        data[0x06:0x08] = (0x0010).to_bytes(2, "little")  # Status bit 4: capabilities list
        data[0x08] = 0xA1  # revision
        data[0x09], data[0x0A], data[0x0B] = 0x00, 0x00, 0x03  # class 030000, VGA
        data[0x0E] = 0x80  # Type 0, multifunction
        data[0x34] = 0x41  # capabilities pointer, with a reserved low bit set
        data[0x2C:0x2E] = (0x1043).to_bytes(2, "little")
        data[0x2E:0x30] = (0x8783).to_bytes(2, "little")
        return ConfigSpace(data=bytes(data))

    def test_identity_fields(self):
        h = decode_common_header(self.build())
        self.assertEqual(h.vendor_id, 0x10DE)
        self.assertEqual(h.device_id, 0x2489)
        self.assertEqual(h.revision_id, 0xA1)

    def test_class_code_packs_three_bytes_high_to_low(self):
        self.assertEqual(decode_common_header(self.build()).class_code, 0x030000)

    def test_capabilities_pointer_masks_the_reserved_low_bits(self):
        self.assertEqual(decode_common_header(self.build()).capabilities_pointer, 0x40)

    def test_status_bit4_is_the_capabilities_list_flag(self):
        self.assertTrue(decode_common_header(self.build()).has_capabilities_list)

    def test_vendor_ffff_means_no_function_present(self):
        h = decode_common_header(space({0x00: 0xFF, 0x01: 0xFF}))
        self.assertFalse(h.function_present)

    def test_subsystem_ids_read_on_type0(self):
        self.assertEqual(subsystem_ids(self.build(), TYPE0), (0x1043, 0x8783))

    def test_subsystem_ids_refused_on_type1(self):
        # 2Ch-2Fh is the prefetchable window on a bridge, not subsystem IDs.
        self.assertIsNone(subsystem_ids(self.build(), TYPE1))


class TestBridgeBusNumbers(unittest.TestCase):
    def test_decodes_three_plain_bytes(self):
        b = decode_type1_header(space({0x18: 0x00, 0x19: 0x01, 0x1A: 0x05}))
        self.assertEqual((b.primary_bus, b.secondary_bus, b.subordinate_bus), (0, 1, 5))

    def test_bus_numbers_are_not_endian_swapped(self):
        # Three separate 8-bit registers. Reading 18h as a u16 would give 0x0100
        # for this input, which is the bug this test exists to catch.
        b = decode_type1_header(space({0x18: 0x00, 0x19: 0x01, 0x1A: 0x01}))
        self.assertEqual(b.secondary_bus, 0x01)

    def test_returns_none_when_the_bytes_were_not_read(self):
        self.assertIsNone(decode_type1_header(ConfigSpace(data=bytes(0x18))))

    def test_available_in_an_unprivileged_64_byte_read(self):
        data = bytearray(64)
        data[0x18], data[0x19], data[0x1A] = 0x00, 0x03, 0x03
        b = decode_type1_header(ConfigSpace(data=bytes(data)))
        self.assertEqual(b.secondary_bus, 3)

    def test_claims_is_inclusive_at_both_ends(self):
        b = Type1Header(primary_bus=0, secondary_bus=2, subordinate_bus=4)
        self.assertFalse(b.claims(1))
        self.assertTrue(b.claims(2))
        self.assertTrue(b.claims(3))
        self.assertTrue(b.claims(4))
        self.assertFalse(b.claims(5))

    def test_unconfigured_is_all_three_zero(self):
        self.assertTrue(Type1Header(0, 0, 0).unconfigured)
        self.assertFalse(Type1Header(0, 1, 1).unconfigured)

    def test_backwards_range_is_invalid_and_claims_nothing(self):
        b = Type1Header(primary_bus=0, secondary_bus=5, subordinate_bus=2)
        self.assertFalse(b.range_valid)
        self.assertFalse(b.claims(3))
        self.assertEqual(b.bus_count, 0)

    def test_bus_count_is_inclusive(self):
        self.assertEqual(Type1Header(0, 2, 4).bus_count, 3)
        self.assertEqual(Type1Header(0, 2, 2).bus_count, 1)

    def test_range_text_matches_lspci_style(self):
        self.assertEqual(Type1Header(0, 1, 1).range_text, "[01]")
        self.assertEqual(Type1Header(0, 4, 7).range_text, "[04-07]")

    def test_leaf_range_is_a_single_bus(self):
        self.assertTrue(Type1Header(0, 1, 1).is_leaf_range)
        self.assertFalse(Type1Header(0, 1, 2).is_leaf_range)


class TestBars(unittest.TestCase):
    def test_32bit_memory_bar(self):
        data = bytearray(256)
        write32(data, 0x10, 0xF0000000)
        bar = decode_bars(ConfigSpace(data=bytes(data)), TYPE0)[0]
        self.assertFalse(bar.is_io)
        self.assertFalse(bar.is_64bit)
        self.assertEqual(bar.base, 0xF0000000)

    def test_io_bar_masks_two_bits(self):
        data = bytearray(256)
        write32(data, 0x10, 0x0000E001)  # bit 0 set: I/O space
        bar = decode_bars(ConfigSpace(data=bytes(data)), TYPE0)[0]
        self.assertTrue(bar.is_io)
        self.assertEqual(bar.base, 0xE000)

    def test_prefetchable_bit(self):
        data = bytearray(256)
        write32(data, 0x10, 0xE0000008)  # bit 3 set
        self.assertTrue(decode_bars(ConfigSpace(data=bytes(data)), TYPE0)[0].prefetchable)

    def test_64bit_bar_joins_the_next_register(self):
        data = bytearray(256)
        write32(data, 0x10, 0xE000000C)  # bits 2:1 = 10 (64-bit), bit 3 prefetchable
        write32(data, 0x14, 0x00000002)  # the high half
        bars = decode_bars(ConfigSpace(data=bytes(data)), TYPE0)
        self.assertTrue(bars[0].is_64bit)
        self.assertEqual(bars[0].base, 0x2E0000000)

    def test_64bit_bar_does_not_leave_a_phantom_bar_behind(self):
        # Six registers, one of them the high half of a 64-bit BAR, must not read
        # as six BARs: the high half is not a BAR of its own.
        data = bytearray(256)
        write32(data, 0x10, 0x0000000C)
        write32(data, 0x14, 0x00000001)
        bars = decode_bars(ConfigSpace(data=bytes(data)), TYPE0)
        self.assertEqual(len(bars), 5)

    def test_zero_bar_is_unimplemented(self):
        bars = decode_bars(space({}), TYPE0)
        self.assertFalse(any(b.implemented for b in bars))

    def test_type1_has_only_two_bars(self):
        self.assertEqual(len(decode_bars(space({}), TYPE1)), 2)

    def test_type0_has_six_bars(self):
        self.assertEqual(len(decode_bars(space({}), TYPE0)), 6)


class TestBridgeWindows(unittest.TestCase):
    def test_memory_window_is_1mb_granular(self):
        data = bytearray(256)
        data[0x20:0x22] = (0x8000).to_bytes(2, "little")  # base bits 31:20 = 800h
        data[0x22:0x24] = (0x80F0).to_bytes(2, "little")  # limit bits 31:20 = 80Fh
        windows = {w.kind: w for w in decode_windows(ConfigSpace(data=bytes(data)), TYPE1)}
        mem = windows["memory"]
        self.assertEqual(mem.base, 0x80000000)
        self.assertEqual(mem.limit, 0x80FFFFFF)  # 80Fh << 20, low 20 bits implied ones
        self.assertTrue(mem.enabled)

    def test_window_with_base_above_limit_is_disabled(self):
        data = bytearray(256)
        data[0x20:0x22] = (0xFFF0).to_bytes(2, "little")
        data[0x22:0x24] = (0x0000).to_bytes(2, "little")
        windows = {w.kind: w for w in decode_windows(ConfigSpace(data=bytes(data)), TYPE1)}
        self.assertFalse(windows["memory"].enabled)

    def test_prefetchable_window_can_be_64bit(self):
        data = bytearray(256)
        data[0x24:0x26] = (0x0001).to_bytes(2, "little")  # low nibble 1: 64-bit
        data[0x26:0x28] = (0x00F1).to_bytes(2, "little")
        write32(data, 0x28, 0x00000004)  # base upper 32
        write32(data, 0x2C, 0x00000004)  # limit upper 32
        windows = {w.kind: w for w in decode_windows(ConfigSpace(data=bytes(data)), TYPE1)}
        pref = windows["prefetchable memory"]
        self.assertTrue(pref.is_64bit)
        self.assertEqual(pref.base, 0x400000000)

    def test_endpoints_have_no_windows(self):
        self.assertEqual(decode_windows(space({}), TYPE0), [])

    def test_window_size_is_inclusive(self):
        self.assertEqual(Window(kind="memory", base=0x0, limit=0xFFFFF).size, 0x100000)


class TestCapabilityChain(unittest.TestCase):
    def chain(self, entries: list[tuple[int, int, int]], size: int = 256) -> ConfigSpace:
        """entries: (offset, cap id, next offset)."""
        data = bytearray(size)
        data[0x06:0x08] = (0x0010).to_bytes(2, "little")
        data[0x34] = entries[0][0] if entries else 0
        for offset, cap_id, next_offset in entries:
            data[offset], data[offset + 1] = cap_id, next_offset
        return ConfigSpace(data=bytes(data))

    def test_walks_to_the_end(self):
        cs = self.chain([(0x40, 0x01, 0x50), (0x50, 0x05, 0x60), (0x60, 0x10, 0x00)])
        caps, problems = walk_capabilities(cs, True)
        self.assertEqual([c.cap_id for c in caps], [0x01, 0x05, 0x10])
        self.assertEqual(problems, [])

    def test_refuses_to_walk_when_status_bit4_is_clear(self):
        cs = self.chain([(0x40, 0x10, 0x00)])
        caps, _ = walk_capabilities(cs, False)
        self.assertEqual(caps, [])

    def test_detects_a_loop_instead_of_hanging(self):
        cs = self.chain([(0x40, 0x01, 0x50), (0x50, 0x05, 0x40)])
        caps, problems = walk_capabilities(cs, True)
        self.assertEqual(len(caps), 2)
        self.assertTrue(any("loops" in p for p in problems))

    def test_rejects_a_pointer_into_the_header(self):
        cs = self.chain([(0x40, 0x01, 0x20)])
        _, problems = walk_capabilities(cs, True)
        self.assertTrue(any("inside the header" in p for p in problems))

    def test_unprivileged_read_truncates_the_chain(self):
        data = bytearray(64)
        data[0x06:0x08] = (0x0010).to_bytes(2, "little")
        data[0x34] = 0x40
        caps, problems = walk_capabilities(ConfigSpace(data=bytes(data)), True)
        self.assertEqual(caps, [])
        self.assertTrue(any("past the 64 bytes" in p for p in problems))

    def test_next_pointer_low_bits_are_masked(self):
        cs = self.chain([(0x40, 0x01, 0x53), (0x50, 0x10, 0x00)])
        caps, _ = walk_capabilities(cs, True)
        self.assertEqual([c.offset for c in caps], [0x40, 0x50])

    def test_finds_the_pcie_capability(self):
        caps = [Capability(0x40, 0x01, 0x50), Capability(0x50, 0x10, 0x00)]
        self.assertEqual(find_pcie_capability(caps).offset, 0x50)

    def test_no_pcie_capability_on_a_plain_pci_function(self):
        self.assertIsNone(find_pcie_capability([Capability(0x40, 0x01, 0x00)]))

    def test_extended_chain_needs_more_than_256_bytes(self):
        self.assertEqual(walk_extended_capabilities(ConfigSpace(data=bytes(256))), [])

    def test_extended_chain_walks_packed_headers(self):
        data = bytearray(4096)
        write32(data, 0x100, (0x140 << 20) | (1 << 16) | 0x0001)
        write32(data, 0x140, (0x000 << 20) | (1 << 16) | 0x0003)
        caps = walk_extended_capabilities(ConfigSpace(data=bytes(data)))
        self.assertEqual([c.cap_id for c in caps], [0x0001, 0x0003])
        self.assertEqual(caps[0].version, 1)


class TestPcieCapability(unittest.TestCase):
    def build(self, port_type=0x4, max_speed=4, max_width=16, cur_speed=4, cur_width=16):
        data = bytearray(256)
        offset = 0x80
        data[offset], data[offset + 1] = 0x10, 0x00
        data[offset + 2 : offset + 4] = ((port_type << 4) | 2).to_bytes(2, "little")
        write32(data, offset + 0x0C, (max_width << 4) | max_speed)
        data[offset + 0x12 : offset + 0x14] = ((cur_width << 4) | cur_speed).to_bytes(2, "little")
        return decode_pcie_capability(
            ConfigSpace(data=bytes(data)), Capability(offset, 0x10, 0x00)
        )

    def test_port_type_comes_from_bits_7_4(self):
        self.assertEqual(self.build(port_type=0x4).port_type_name, "Root Port")
        self.assertEqual(self.build(port_type=0x0).port_type_name, "Endpoint")
        self.assertEqual(self.build(port_type=0x6).port_type_name, "Downstream Port")
        self.assertEqual(
            self.build(port_type=0x9).port_type_name, "Root Complex Integrated Endpoint"
        )

    def test_link_capabilities_and_status(self):
        cap = self.build(max_speed=4, max_width=16, cur_speed=1, cur_width=16)
        self.assertEqual(cap.maximum.speed, 4)
        self.assertEqual(cap.maximum.width, 16)
        self.assertEqual(cap.current.speed, 1)

    def test_slower_than_maximum_is_degraded(self):
        cap = self.build(max_speed=4, cur_speed=1)
        self.assertTrue(cap.speed_degraded)
        self.assertTrue(cap.degraded)
        self.assertIn("2.5 GT/s of 16 GT/s", cap.degraded_reason)

    def test_narrower_than_maximum_is_degraded(self):
        cap = self.build(max_width=16, cur_width=4)
        self.assertTrue(cap.width_degraded)
        self.assertIn("x4 of x16", cap.degraded_reason)

    def test_full_speed_and_width_is_not_degraded(self):
        self.assertFalse(self.build().degraded)

    def test_empty_slot_is_not_degraded(self):
        # Width 0 means nothing trained. Calling that "degraded" would flag every
        # empty slot on the machine as a fault.
        cap = self.build(cur_width=0, cur_speed=1)
        self.assertFalse(cap.degraded)
        self.assertFalse(cap.link_up)

    def test_integrated_endpoint_has_no_link_registers(self):
        cap = self.build(port_type=0x9)
        self.assertIsNone(cap.maximum)
        self.assertIsNone(cap.current)
        self.assertFalse(cap.has_link)

    def test_link_text_shows_current_and_maximum(self):
        self.assertEqual(
            self.build(max_speed=4, cur_speed=1).link_text,
            "2.5 GT/s x16 (max 16 GT/s x16)",
        )


class TestSpeedAndWidthText(unittest.TestCase):
    def test_speed_encodings(self):
        self.assertEqual(speed_text(1), "2.5 GT/s")
        self.assertEqual(speed_text(3), "8 GT/s")
        self.assertEqual(speed_text(5), "32 GT/s")

    def test_zero_speed_is_unknown_not_a_number(self):
        self.assertEqual(speed_text(0), "unknown")

    def test_reserved_encoding_is_labelled(self):
        self.assertIn("reserved", speed_text(15))

    def test_width_zero_says_no_link(self):
        self.assertIn("no link", width_text(0))
        self.assertEqual(width_text(16), "x16")

    def test_link_state_text(self):
        self.assertEqual(LinkState(speed=4, width=16).text, "16 GT/s x16")
        self.assertEqual(LinkState(speed=4, width=16).gen, "Gen4")


class TestPcieCapabilityFlags(unittest.TestCase):
    def test_no_link_means_no_degradation_verdict(self):
        cap = PcieCapability(offset=0x40, version=2, port_type=0x9, slot_implemented=False)
        self.assertFalse(cap.degraded)
        self.assertEqual(cap.link_text, "")


if __name__ == "__main__":
    unittest.main()
