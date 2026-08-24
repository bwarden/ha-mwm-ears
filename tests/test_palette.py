"""Tests for mwm.palette: tables and nearest-color snapping."""

"""Test bootstrap: expose the integration package before imports."""

import _bootstrap  # noqa: F401  (must precede mwm imports)

import unittest

from mwm.palette import PALETTE, SIMPLE_COLORS, nearest_entry


class TableTests(unittest.TestCase):
    def test_simple_colors_complete(self):
        self.assertEqual(len(SIMPLE_COLORS), 7)
        self.assertEqual(SIMPLE_COLORS[0x64], ("red", (0xFF, 0x00, 0x00)))

    def test_palette_matches_verified_tsv(self):
        # Spot-check rig-measured RGB values from samples/mwm-gwts-colors.tsv.
        self.assertEqual(PALETTE[0x00][1], (0xAC, 0xFE, 0xFE))
        self.assertEqual(PALETTE[0x0B][1], (0xFE, 0x0D, 0xFF))
        self.assertEqual(PALETTE[0x15][1], (0xFF, 0x00, 0x00))
        self.assertEqual(PALETTE[0x1C][1], (0xFE, 0xFE, 0xFE))

    def test_palette_excludes_off_entry_from_matching(self):
        # 0x1D is black/off: present in the protocol but never offered
        # as a color choice, so it stays out of the match table.
        self.assertEqual(len(PALETTE), 29)
        self.assertEqual(max(PALETTE), 0x1C)


class NearestTests(unittest.TestCase):
    def test_pure_red_snaps_to_simple_red(self):
        # Exact simple red ties palette 0x15; simple wins ties because
        # composed frames can set it per-ear. Near-red shades legitimately
        # snap to measured palette entries instead.
        kind, code = nearest_entry((255, 0, 0))
        self.assertEqual((kind, code), ("simple", 0x64))

    def test_deep_saturated_blue_prefers_simple(self):
        kind, code = nearest_entry((0, 0, 255))
        self.assertEqual((kind, code), ("simple", 0x61))

    def test_pastel_shade_uses_palette(self):
        kind, _ = nearest_entry((0xAC, 0xFE, 0xFE))
        self.assertEqual(kind, "palette")

    def test_palette_sky_exact_match(self):
        _, code = nearest_entry((172, 254, 254))
        self.assertEqual(code, 0x00)

    def test_crimson_over_magenta(self):
        _, code = nearest_entry((255, 0, 24))
        self.assertEqual(code, 0x0E)  # crimson FF0011

    def test_result_always_representable(self):
        for rgb in [(12, 34, 56), (200, 100, 50), (128, 128, 128)]:
            kind, code = nearest_entry(rgb)
            if kind == "palette":
                self.assertIn(code, PALETTE)
            else:
                self.assertIn(code, SIMPLE_COLORS)


if __name__ == "__main__":
    unittest.main()
