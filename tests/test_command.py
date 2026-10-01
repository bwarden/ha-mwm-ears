"""Tests for the MwmCommand envelope handed to infrared emitters."""

"""Test bootstrap: expose the integration package before imports."""

import _bootstrap  # noqa: F401  (must precede mwm imports)

import unittest

from mwm import CARRIER_HZ, MwmCommand, build_frame, raw_timings


class MwmCommandTests(unittest.TestCase):
    def test_carrier_is_38khz(self):
        cmd = MwmCommand(build_frame([0x60]))
        self.assertEqual(cmd.modulation, CARRIER_HZ)
        self.assertEqual(cmd.modulation, 38000)

    def test_raw_timings_passthrough(self):
        frame = build_frame([0x61, 0x66])
        cmd = MwmCommand(frame)
        self.assertEqual(cmd.get_raw_timings(), raw_timings(frame))
        self.assertEqual(cmd.frame, frame)

    def test_space_stretch_lengthens_spaces_not_marks(self):
        """Tuya S18 compensation adds to spaces (negative runs) only."""
        frame = build_frame([0x61, 0x66])
        base = raw_timings(frame)
        comp = MwmCommand(frame, space_stretch_us=88).get_raw_timings()
        self.assertEqual(len(base), len(comp))
        for i, (b, c) in enumerate(zip(base, comp)):
            if i % 2 == 0:  # mark: unchanged
                self.assertEqual(c, b)
            else:  # space: 88us longer in magnitude
                self.assertEqual(abs(c) - abs(b), 88)

    def test_space_stretch_zero_is_passthrough(self):
        frame = build_frame([0x60])
        self.assertEqual(
            MwmCommand(frame, space_stretch_us=0).get_raw_timings(),
            raw_timings(frame),
        )

    def test_repeat_count_default_zero(self):
        self.assertEqual(MwmCommand(build_frame([0x60])).repeat_count, 0)
        self.assertEqual(
            MwmCommand(build_frame([0x60]), repeat_count=2).repeat_count, 2
        )

    def test_repr_shows_hex(self):
        frame = build_frame([0x60])
        self.assertIn(frame.hex().upper(), repr(MwmCommand(frame)))


if __name__ == "__main__":
    unittest.main()
