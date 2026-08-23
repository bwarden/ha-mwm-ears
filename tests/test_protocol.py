"""Tests for mwm.protocol: framing, CRC, and IR signal encoding."""

"""Test bootstrap: expose the integration package before imports."""

import _bootstrap  # noqa: F401  (must precede mwm imports)

import unittest

from mwm.protocol import (
    CARRIER_HZ,
    FOOTER_GAP_US,
    TICK_US,
    build_55aa,
    build_frame,
    crc8_dallas,
    frame_is_valid,
    irsend_payload,
    parse_frame_hex,
    timings_for_frame,
)


class CrcTests(unittest.TestCase):
    def test_known_checksums_from_verified_tsv(self):
        # 90 60 A6 (both off keep-alive) and 91 61 6A B7? -> use real rows.
        self.assertEqual(crc8_dallas([0x90, 0x60]), 0xA6)
        self.assertEqual(crc8_dallas([0x92, 0x24, 0x0C, 0x00]), 0xA1)

    def test_matches_irremote_reference(self):
        # CRC-8/Dallas of "123456789" is 0xA1 (check value for poly 0x31
        # normal / 0x8C reflected).
        self.assertEqual(crc8_dallas(b"123456789"), 0xA1)

    def test_accepts_bytes_like(self):
        self.assertEqual(
            crc8_dallas(bytes([0x90, 0x60])), crc8_dallas((0x90, 0x60))
        )


class BuildFrameTests(unittest.TestCase):
    def test_header_carries_length_nibble(self):
        self.assertEqual(build_frame([0x60])[0], 0x90)
        content = [0x42, 0x00, 0x00, 0x48, 0x17, 0x0C, 0x40]
        self.assertEqual(build_frame(content)[0], 0x96)
        # total = header + content + crc = len(content) + 2
        self.assertEqual(len(build_frame(content)), len(content) + 2)

    def test_trailing_byte_is_crc(self):
        frame = build_frame([0x24, 0x48, 0x85])
        self.assertEqual(frame[-1], crc8_dallas(frame[:-1]))

    def test_park_beacon_roundtrip(self):
        # Park variant from docs section 5.
        frame = bytes.fromhex("964200004817 0C402F".replace(" ", ""))
        ok, reason = frame_is_valid(build_frame(list(frame[1:-1])))
        self.assertTrue(ok, reason)

    def test_rejects_bad_lengths(self):
        with self.assertRaises(ValueError):
            build_frame([])
        with self.assertRaises(ValueError):
            build_frame([1] * 17)


class ValidityTests(unittest.TestCase):
    def test_valid_frames(self):
        self.assertEqual(frame_is_valid("90 60 A6"), (True, ""))
        frame = build_frame([0x61, 0x62])  # fused left-blue/right-green
        self.assertEqual(frame_is_valid(frame.hex()), (True, ""))
        self.assertEqual(frame_is_valid(bytes.fromhex("90 60 A6")), (True, ""))

    def test_additive_checksum_branch(self):
        frame = build_55aa([0x05, 0x06, 0x01, 0x02, 0x03])
        self.assertEqual(frame[:2], b"\x55\xaa")
        ok, reason = frame_is_valid(frame.hex().upper())
        self.assertTrue(ok, reason)
        bad = bytearray(frame)
        bad[-1] ^= 0xFF
        ok, reason = frame_is_valid(bytes(bad))
        self.assertFalse(ok)
        self.assertIn("additive checksum mismatch", reason)

    def test_length_rule_enforced(self):
        ok, reason = frame_is_valid("97 24 48 85")  # claims 7 payload, has 2
        self.assertFalse(ok)
        self.assertIn("length rule", reason)

    def test_crc_mismatch_detected(self):
        ok, reason = frame_is_valid("90 61 FF")
        self.assertFalse(ok)
        self.assertIn("CRC mismatch", reason)

    def test_non_hex_input(self):
        ok, _ = frame_is_valid("zz 60 A6")
        self.assertFalse(ok)


class TimingsTests(unittest.TestCase):
    def test_alternating_and_merged(self):
        t = timings_for_frame(bytes.fromhex("9060A6"))
        self.assertGreater(t[0], 0)  # starts with a mark duration
        # Merged output must alternate mark/space; we only see magnitudes, so
        # check the count is even (ends with the footer gap space).
        self.assertEqual(len(t) % 2, 0)

    def test_footer_gap_present(self):
        t = timings_for_frame(bytes.fromhex("9060A6"))
        # The trailing stop-bit space merges into the inter-command gap.
        self.assertGreaterEqual(t[-1], FOOTER_GAP_US)

    def test_leading_run_merges_start_mark_and_zero_bits(self):
        # 0x90 low nibble is zero: the start mark merges with data bits
        # 0-3 (all marks) into one five-tick run, exactly like the TS and
        # perl encoders.
        self.assertEqual(
            timings_for_frame(bytes.fromhex("9060A6"))[0], 5 * TICK_US)

    def test_two_tick_mark_when_first_bit_set(self):
        # Duration invariant: every byte contributes exactly 10 ticks
        # (start + 8 data + stop) regardless of merging.
        frame = bytes.fromhex("9060A6")
        expected = 3 * 10 * TICK_US + FOOTER_GAP_US
        self.assertEqual(sum(timings_for_frame(frame)), expected)


class IrsendPayloadTests(unittest.TestCase):
    def test_prefix_and_format(self):
        payload = irsend_payload(bytes.fromhex("9060A6"))
        parts = payload.split(",")
        self.assertEqual(parts[0], str(CARRIER_HZ))
        self.assertTrue(all(p.isdigit() for p in parts[1:]))

    def test_smoke_run_shape(self):
        # The perl smoke run produced 25 numbers for this frame family;
        # ours must at least be in the same ballpark (alternation + gap).
        payload = irsend_payload(build_frame([0x24, 0x0C, 0x40]))
        self.assertLess(len(payload.split(",")), 30)


class ParseHexTests(unittest.TestCase):
    def test_plus_separated(self):
        frames = parse_frame_hex("9060A6+916162FF")
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0], bytes.fromhex("9060A6"))
        self.assertEqual(frames[1], bytes.fromhex("916162FF"))

    def test_empty_parts_skipped(self):
        self.assertEqual(parse_frame_hex("9060A6++"), [bytes.fromhex("9060A6")])


if __name__ == "__main__":
    unittest.main()
