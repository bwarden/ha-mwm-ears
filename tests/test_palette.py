"""Tests for mwm.palette: tables and nearest-color snapping."""

"""Test bootstrap: expose the integration package before imports."""

import _bootstrap  # noqa: F401  (must precede mwm imports)

import unittest

from mwm.palette import PALETTE, SIMPLE_COLORS, color_palette, nearest_entry


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


class SnapQualityTests(unittest.TestCase):
    """Hue-dominant snapping: perceptual 'far away' means wrong hue."""

    @staticmethod
    def _hue(rgb):
        import colorsys

        r, g, b = (v / 255 for v in rgb)
        return colorsys.rgb_to_hsv(r, g, b)[0] * 360

    def test_dark_muted_requests_keep_their_hue(self):
        # RGB-distance sent brown to blazing yellow-green etc.; users
        # forgive brightness drift, never hue drift.
        for target in ((139, 69, 19),   # brown
                       (128, 0, 32),    # maroon
                       (128, 128, 0)):  # olive
            kind, code = nearest_entry(target)
            table = SIMPLE_COLORS if kind == "simple" else PALETTE
            delta = abs(self._hue(target) - self._hue(table[code][1]))
            delta = min(delta, 360 - delta)
            self.assertLessEqual(delta, 15.0, f"{target} lost its hue")

    def test_grey_lands_on_white_not_a_pale_tint(self):
        self.assertEqual(nearest_entry((128, 128, 128)), ("palette", 0x1C))

    def test_saturated_teal_stays_cyan(self):
        self.assertEqual(nearest_entry((0, 128, 128)), ("simple", 0x63))


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


class ColorPaletteTests(unittest.TestCase):
    def test_lists_every_representable_colour(self):
        # 7 simple one-bit colours + the 30 palette shades minus the
        # black/off entry (0x1D).
        catalog = color_palette()
        self.assertEqual(len(catalog), 7 + 29)
        self.assertEqual(len([c for c in catalog if c["kind"] == "simple"]), 7)
        self.assertEqual(len([c for c in catalog if c["kind"] == "palette"]), 29)

    def test_simple_entry_shape(self):
        entry = next(c for c in color_palette() if c["kind"] == "simple")
        self.assertEqual(len(entry["rgb"]), 3)
        self.assertIsInstance(entry["name"], str)
        self.assertIn("code", entry)
        self.assertNotIn("index", entry)

    def test_palette_entry_shape(self):
        entry = next(c for c in color_palette() if c["kind"] == "palette")
        self.assertEqual(len(entry["rgb"]), 3)
        self.assertIsInstance(entry["name"], str)
        self.assertIn("index", entry)
        self.assertNotIn("code", entry)

    def test_rgb_matches_palette_tables(self):
        catalog = color_palette()
        for entry in catalog:
            if entry["kind"] == "simple":
                self.assertEqual(entry["rgb"], list(SIMPLE_COLORS[entry["code"]][1]))
            else:
                self.assertEqual(entry["rgb"], list(PALETTE[entry["index"]][1]))

    def test_excludes_black_off(self):
        names = [c["name"] for c in color_palette()]
        self.assertNotIn("black/off", names)
        palette_names = [
            c["name"] for c in color_palette() if c["kind"] == "palette"
        ]
        self.assertNotIn("black/off", palette_names)

    def test_off_not_offered(self):
        # 0x1D (off) must never appear as a pickable shade.
        self.assertNotIn(0x1D, [c.get("index") for c in color_palette()])



if __name__ == "__main__":
    unittest.main()
