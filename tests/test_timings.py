"""Tests for mwm.timings: decoding framework-format raw timings."""

import unittest

from mwm import build_frame, decode_timings, frame_is_valid, raw_timings
from mwm.timings import DELTA_US, TICK_US, MAX_WIDTH_TICKS


class RoundtripTests(unittest.TestCase):
    def test_simple_frame_roundtrip(self):
        for content in ([0x60], [0x61, 0x66], [0x24, 0x48, 0x85]):
            frame = build_frame(content)
            self.assertEqual(decode_timings(raw_timings(frame)), [frame])

    def test_long_frame_roundtrip(self):
        # 10-byte beacon with the D0 clause: exercises merged runs heavily.
        content = [0x42, 0x00, 0x00, 0x48, 0x14, 0x0C, 0xCA, 0xD0, 0x0E, 0xA1]
        frame = build_frame(content)
        self.assertEqual(decode_timings(raw_timings(frame)), [frame])

    def test_55aa_roundtrip(self):
        from mwm import build_55aa

        frame = build_55aa([0x05, 0x06, 0x04, 0x01, 0x02])
        self.assertEqual(frame_is_valid(frame), (True, ""))
        self.assertEqual(decode_timings(raw_timings(frame)), [frame])


class RobustnessTests(unittest.TestCase):
    def test_leading_gap_tolerated(self):
        frame = build_frame([0x60])
        signal = [-40000] + raw_timings(frame)
        self.assertEqual(decode_timings(signal), [frame])

    def test_multi_frame_capture_splits_on_gap(self):
        frames = [build_frame([0x60]), build_frame([0x64])]
        signal = list(raw_timings(frames[0]))
        # Strip the footer gap and re-attach it as an inter-message gap.
        signal[-1] = -30000
        signal += raw_timings(frames[1])
        self.assertEqual(decode_timings(signal), frames)

    def test_repeated_burst_decodes_all_copies(self):
        frame = build_frame([0x61, 0x66])
        signal = []
        for _ in range(3):
            runs = raw_timings(frame)
            signal.extend(runs[:-1])
            signal.append(-30000)  # each copy ends in a fresh gap
        self.assertEqual(decode_timings(signal), [frame] * 3)

    def test_truncated_message_dropped(self):
        frame = build_frame([0x24, 0x48, 0x85])
        runs = raw_timings(frame)
        truncated = runs[: len(runs) // 2]
        self.assertEqual(decode_timings(truncated), [])

    def test_corrupt_width_abandons_message(self):
        frame = build_frame([0x60])
        runs = raw_timings(frame)
        runs[2] += 5000  # matches no tick count
        self.assertEqual(decode_timings(runs), [])

    def test_bad_checksum_dropped(self):
        good = build_frame([0x60])
        bad = bytearray(good)
        bad[-1] ^= 0xFF
        self.assertEqual(decode_timings(raw_timings(bytes(bad))), [])


class ConventionTests(unittest.TestCase):
    def test_raw_timings_signed_alternating(self):
        runs = raw_timings(build_frame([0x60]))
        self.assertGreater(runs[0], 0)  # starts with a mark
        for i, value in enumerate(runs):
            if i % 2 == 0:
                self.assertGreater(value, 0)
            else:
                self.assertLess(value, 0)

    def test_merged_runs_within_tolerance(self):
        # Every run must match a whole number of ticks within kDELTA.
        runs = raw_timings(build_frame([0x42, 0x00, 0x00, 0x48, 0x14,
                                        0x0C, 0xCA, 0xD0, 0x0E, 0xA1]))
        for width in (abs(v) for v in runs[:-1]):  # skip footer gap
            ticks = round(width / TICK_US)
            self.assertTrue(
                any(
                    abs(width - t * TICK_US) <= DELTA_US
                    for t in range(1, MAX_WIDTH_TICKS + 1)
                ),
                f"width {width} matches no tick count",
            )


if __name__ == "__main__":
    unittest.main()
