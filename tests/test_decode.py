"""Tests for mwm.decode: phrase description and ear-state tracking."""

"""Test bootstrap: expose the integration package before imports."""

import _bootstrap  # noqa: F401  (must precede mwm imports)

import unittest

from mwm.decode import (
    EarStateTracker,
    describe_55aa,
    describe_bundle,
    describe_content,
    describe_frame,
    effect_label,
)
from mwm.protocol import build_frame


def frame_hex(content):
    return build_frame(content).hex().upper()


class EffectLabelTests(unittest.TestCase):
    def test_known_labels(self):
        self.assertEqual(effect_label(0x85), "fade out")
        self.assertEqual(effect_label(0x83), "crossfading transitions")
        self.assertEqual(effect_label(0x80), "power-on display (demo mode entry)")

    def test_unlabeled_sequences(self):
        self.assertEqual(effect_label(0x88), "color sequence 0x88")
        self.assertEqual(effect_label(0x2B), "effect 0x2b")


class DescribeContentTests(unittest.TestCase):
    def test_both_ears_color(self):
        desc = describe_content([0x66])
        self.assertEqual(desc["kind"], "color-command")
        self.assertIn("both ears yellow", desc["summary"])

    def test_fused_phrase_tokens(self):
        desc = describe_content([0x61, 0x66])
        self.assertEqual(len(desc["tokens"]), 2)

    def test_effect_invocation(self):
        desc = describe_content([0x24, 0x48, 0x85])
        self.assertEqual(desc["kind"], "effect-command")
        self.assertIs(desc["effect"], 0x85)
        self.assertIn("invoke fade out", desc["summary"])
        self.assertIn("reset/override", desc["summary"])

    def test_delay_and_timer_opcodes(self):
        desc = describe_content([0xF5, 0x58, 0x32])
        self.assertIn("delay ~500 ms", desc["summary"])
        self.assertIn("cycle timer 0x32", desc["summary"])

    def test_d0_modifier_consumes_two_args(self):
        desc = describe_content([0xD0, 0x42, 0x1E])
        self.assertEqual(desc["tokens"], ["D0 modifier (0x42,0x1e)"])

    def test_unknown_byte_tokenized_not_crash(self):
        desc = describe_content([0xEE])
        self.assertTrue(desc["summary"].startswith("unknown"))


class BeaconTests(unittest.TestCase):
    LIVE_BEACON = [0x42, 0x00, 0x00, 0x48, 0x14, 0x0C, 0xCA, 0xD0, 0x0E, 0xA1]
    PARK_BEACON = [0x42, 0x00, 0x00, 0x48, 0x17, 0x0C, 0x40]

    def test_live_variant_recognised(self):
        desc = describe_content(self.LIVE_BEACON)
        self.assertEqual(desc["kind"], "beacon")
        # ss=0x14 has no verified name yet; the generic label still shows.
        self.assertIn("idle beacon (demo effect running: effect 0x14)",
                      desc["summary"])

    def test_park_variant_recognised(self):
        desc = describe_content(self.PARK_BEACON)
        self.assertEqual(desc["kind"], "beacon")
        self.assertIn("effect 0x17", desc["summary"])

    def test_named_demo_effect_labelled(self):
        body = [0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40]
        self.assertIn("color sequence 0x88",
                      describe_content(body)["summary"])

    def test_near_beacon_rejected(self):
        # Same shape but constant block disturbed -> not a beacon.
        body = list(self.LIVE_BEACON)
        body[1] = 0x01
        desc = describe_content(body)
        self.assertNotEqual(desc["kind"], "beacon")


class FrameDescriptionTests(unittest.TestCase):
    def test_invalid_frame_reported(self):
        desc = describe_frame("90 60 FF")
        self.assertEqual(desc["kind"], "invalid")

    def test_color_command_from_wire_hex(self):
        desc = describe_frame(frame_hex([0x60]))
        self.assertEqual(desc["kind"], "color-command")
        self.assertIn("both ears off", desc["summary"])

    def test_game_message(self):
        frame = bytes([0x55, 0xAA, 0x05, 0x06, 0x04, 0x01, 0x02,
                       sum([0x05, 0x06, 0x04, 0x01, 0x02]) % 256])
        desc = describe_55aa(frame)
        self.assertEqual(desc["summary"], "interactive game: laser tag")

    def test_shutdown_message(self):
        frame = bytes.fromhex("55AA08C413FF01EDAFF F7A".replace(" ", ""))
        desc = describe_frame(frame.hex())
        self.assertEqual(desc["summary"], "ride shutdown command")


class TrackerTests(unittest.TestCase):
    def test_colour_script_read_per_slot(self):
        # NOTE: rig session 2026-08-23 showed multi-byte phrases EXECUTE
        # sequentially (last opcode wins on real ears). The tracker still
        # reads slots independently for room-state display; revisit when
        # genuine receiver traffic clarifies who sends such phrases.
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x61, 0x66]))
        self.assertEqual(tracker.snapshot(), "left blue, right yellow")
        self.assertIsNone(tracker.effect)

    def test_right_only_frames_touch_right_slot_only(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x61]))          # both blue
        tracker.feed_frame(build_frame([0x6E]))        # right-only yellow
        self.assertEqual(tracker.snapshot(), "left blue, right yellow")
        tracker.feed_frame(build_frame([0x68]))        # right-only off
        self.assertEqual(tracker.snapshot(), "left blue, right off")

    def test_short_palette_forms_read_per_side(self):
        tracker = EarStateTracker()
        tracker.feed_frame(build_frame([0x0E, 0x80 | 0x01]))  # right sky blue
        self.assertEqual(tracker.snapshot(), "left off, right sky blue")
        tracker.feed_frame(build_frame([0x0E, 0x12]))         # both pure yellow
        self.assertEqual(tracker.snapshot(), "both ears pure yellow")

    def test_both_off_keepalive(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x61, 0x62]))
        tracker.feed_frame("90 60 A6")
        self.assertEqual(tracker.snapshot(), "both ears off")

    def test_blackout_reset(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x63, 0x63]))
        tracker.feed_frame(frame_hex([0x24]))
        self.assertEqual(tracker.snapshot(), "both ears off")

    def test_effect_recorded_and_color_kept(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x64, 0x64]))
        tracker.feed_frame(frame_hex([0x48, 0x84]))
        snap = tracker.snapshot()
        self.assertIn("both ears red", snap)
        self.assertIn("running: strobe flashes into running program", snap)

    def test_beacon_updates_only_effect(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x65, 0x65]))
        beacon = build_frame(
            [0x42, 0x00, 0x00, 0x48, 0x14, 0x0C, 0x40]
        ).hex().upper()
        desc = tracker.feed_frame(beacon)
        self.assertEqual(desc["kind"], "beacon")
        snap = tracker.snapshot()
        self.assertIn("both ears magenta", snap)
        self.assertIn("running:", snap)

    def test_palette_template_sets_both(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x19, 0x07, 0x0F, 0x16, 0x0E, 0x18, 0x04]))
        snap = tracker.snapshot()
        self.assertIn("both ears scarlet", snap)

    def test_invalid_frame_leaves_state_alone(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x64, 0x64]))
        desc = tracker.feed_frame("90 60 FF")
        self.assertEqual(desc["kind"], "invalid")
        self.assertIn("both ears red", tracker.snapshot())

    def test_last_summary_exposed(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x60]))
        self.assertIn("both ears off", tracker.last_summary)


if __name__ == "__main__":
    unittest.main()


class BundleTests(unittest.TestCase):
    """A-B-A' wand/ear command bundles (doc section 3)."""

    def test_recognises_phrase_companion_phrase(self):
        phrase = build_frame([0x61, 0x6A])          # two-opcode color script
        companion = build_frame(                    # pulse w/ cycle params
            [0x24, 0x58, 0xF0, 0x48, 0x04, 0xD0, 0x42, 0x0A]
        )
        out = describe_bundle([phrase, companion, phrase])
        self.assertIsNotNone(out)
        self.assertEqual(out["kind"], "bundle")
        self.assertIn("pulse", out["summary"].lower())
        self.assertEqual(out["phrase_hex"], phrase.hex().upper())
        self.assertEqual(out["companion_hex"], companion.hex().upper())
        self.assertIn("special cycle arg", out["summary"])   # 58 F0
        self.assertIn("cycle scale 10 x 200 ms", out["summary"])

    def test_bundle_detected_with_rolling_counter_tail(self):
        """Real rigs mutate a counter byte (+CRC) between A and A'."""
        base = bytes.fromhex("961908091272A2")
        phrase_a = build_frame(base + bytes([0x00]))
        phrase_a2 = build_frame(base + bytes([0xB3]))
        companion = build_frame(bytes([0x24, 0x0C, 0x72]))
        out = describe_bundle([phrase_a, companion, phrase_a2])
        self.assertIsNotNone(out)
        self.assertEqual(out["kind"], "bundle")
        self.assertEqual(out["phrase_hex"], phrase_a.hex().upper())

    def test_short_phrases_still_require_exact_match(self):
        """Tail exemption must not fuse distinct short color commands."""
        off = build_frame([0x60])          # canonical 90 60 A6
        blue = build_frame([0x61])
        self.assertIsNone(describe_bundle([off, blue, blue]))

    def test_identical_short_triple_is_a_bundle(self):
        phrase = build_frame([0x60])
        companion = build_frame([0x24, 0x58, 0xF0])
        out = describe_bundle([phrase, companion, phrase])
        self.assertIsNotNone(out)
        self.assertEqual(out["companion_hex"], companion.hex().upper())

    def test_rejects_non_bundles(self):
        a = build_frame([0x60])
        b = build_frame([0x61])
        self.assertIsNone(describe_bundle([a]))
        self.assertIsNone(describe_bundle([a, b]))
        self.assertIsNone(describe_bundle([a, a, a]))   # all identical
        self.assertIsNone(describe_bundle([a, b, b]))   # A' mismatch
