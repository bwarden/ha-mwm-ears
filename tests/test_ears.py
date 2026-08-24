"""Tests for the HA-free ear-pair state logic (refresh/off/suspend rules)."""

import unittest

from mwm import build_frame

# Load ears.py without importing the integration package (its __init__
# pulls in homeassistant). Same trick as tests/_bootstrap.py.
import importlib.util
import pathlib
import sys

_EARS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components" / "mwm_ears" / "ears.py"
)
_spec = importlib.util.spec_from_file_location("ears_core", _EARS)
ears = importlib.util.module_from_spec(_spec)
sys.modules["ears_core"] = ears
_spec.loader.exec_module(ears)


class Harness:
    """Records transmissions against a controllable clock."""

    def __init__(self):
        self.now = 100.0
        self.sent: list[tuple[str, int]] = []  # (hex, repeat_count)
        self.pair = ears.EarPairState(
            self._transmit, clock=lambda: self.now, repeat_gap_s=0
        )

    async def _transmit(self, frame: bytes, repeat_count: int) -> None:
        self.sent.append((frame.hex().upper(), repeat_count))

    @property
    def calls(self):
        return self.sent


def run(coro):
    import asyncio

    return asyncio.run(coro)


class ApplyTests(unittest.TestCase):
    def test_initial_send_is_multi_burst(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        # override once, then the composed group (both-red + right-only
        # off-restorer) re-transmitted BURST_REPEATS+1 times.
        self.assertEqual(len(h.calls), 1 + 2 * (ears.BURST_REPEATS + 1))
        reset_hex, reset_repeats = h.calls[0]
        self.assertEqual(reset_repeats, 0)
        self.assertEqual(bytes.fromhex(reset_hex)[1], 0x24)
        group = h.calls[1:]
        first, second = group[0], group[1]
        self.assertEqual(bytes.fromhex(first[0]), build_frame([0x64]))
        self.assertEqual(
            bytes.fromhex(second[0]), build_frame([0x68])
        )  # right-only OFF keeps left red
        for i, (_hexa, rc) in enumerate(group):
            self.assertEqual(rc, 0)
            expected = first if i % 2 == 0 else second
            self.assertEqual(_hexa, expected[0])

    def test_other_side_color_preserved_by_composition(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x61))
        h.calls.clear()
        run(h.pair.apply_simple("right", 0x66))
        # Bring BOTH ears to blue, then repaint only the right ear yellow.
        self.assertEqual(
            h.calls[-2:], [(build_frame([0x61]).hex().upper(), 0),
                           (build_frame([0x6E]).hex().upper(), 0)]
        )

    def test_palette_template_frame_shape(self):
        h = Harness()
        run(h.pair.apply_palette(0x0E))
        frame = bytes.fromhex(h.calls[-1][0])
        # header 19 07 0F 16 pp 18 04 crc
        self.assertEqual(len(frame), 9)
        self.assertEqual(frame[1:6], bytes([0x19, 0x07, 0x0F, 0x16, 0x0E]))
        self.assertEqual(frame[-3:-1], bytes([0x18, 0x04]))

    def test_effect_invoke_carries_24_prefix(self):
        h = Harness()
        run(h.pair.apply_effect(0x84, "Strobe flash"))
        frame = bytes.fromhex(h.calls[0][0])
        self.assertEqual(list(frame[1:-1]), [0x24, 0x48, 0x84])


class TurnOnRestoreTests(unittest.TestCase):
    def test_fresh_pair_defaults_to_white(self):
        h = Harness()
        sent = run(h.pair.turn_on_side("left"))
        self.assertEqual(sent, 0x67)
        frame = bytes.fromhex(h.calls[-2][0])
        self.assertEqual(frame[1], 0x67)
        self.assertEqual(bytes.fromhex(h.calls[-1][0]), build_frame([0x68]))

    def test_restores_last_explicit_color(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))   # red
        run(h.pair.turn_off_side("left"))
        before = len(h.calls)
        sent = run(h.pair.turn_on_side("left"))
        self.assertEqual(sent, 0x64)
        # override + composed group x3; group leads with the colour.
        self.assertEqual(len(h.calls),
                         before + 1 + 2 * (ears.BURST_REPEATS + 1))
        self.assertEqual(bytes.fromhex(h.calls[before][0])[1], 0x24)
        self.assertEqual(bytes.fromhex(h.calls[before + 1][0]),
                         build_frame([0x64]))
        self.assertEqual(bytes.fromhex(h.calls[-1][0]), build_frame([0x68]))

    def test_sides_remember_independently(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.apply_simple("right", 0x61))
        self.assertEqual(run(h.pair.turn_on_side("right")), 0x61)
        # right's pick must not leak into left
        self.assertNotEqual(run(h.pair.turn_on_side("left")), 0x61)


class CompositionTests(unittest.TestCase):
    """Per-side control = coordination of BOTH + RIGHT-only primitives."""

    def test_right_only_codes_mirror_simple_codes(self):
        # TSV rows color-X-right: exact CRCs pin the mapping 90 68..6F.
        expected = {0x60: "906864", 0x64: "906C05",
                    0x66: "906EB9", 0x67: "906FE7"}
        RIGHT_ONLY_BASE, EAR_OFF_CODE = ears.RIGHT_ONLY_BASE, ears.EAR_OFF_CODE
        for simple, hexstr in expected.items():
            frame = build_frame([RIGHT_ONLY_BASE + simple - EAR_OFF_CODE])
            self.assertEqual(frame.hex().upper(), hexstr)

    def test_mixed_pair_composes_both_then_right_only(self):
        RIGHT_ONLY_BASE, EAR_OFF_CODE = ears.RIGHT_ONLY_BASE, ears.EAR_OFF_CODE
        from ears_core import LEFT, RIGHT
        h = Harness()
        h.pair.codes = {LEFT: 0x62, RIGHT: 0x66}
        frames = h.pair._state_frames()
        self.assertEqual(frames[0], build_frame([0x62]))
        self.assertEqual(
            frames[1], build_frame([RIGHT_ONLY_BASE + 0x66 - EAR_OFF_CODE])
        )

    def test_left_dark_right_lit_needs_all_off_then_right_only(self):
        from ears_core import LEFT, RIGHT
        h = Harness()
        h.pair.codes = {LEFT: 0x60, RIGHT: 0x63}
        frames = h.pair._state_frames()
        self.assertEqual(frames[0], build_frame([0x60]))
        self.assertEqual(frames[1], build_frame([0x6B]))

    def test_group_repeats_preserve_frame_order(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        state_calls = h.calls[1:]  # skip override
        pattern = [c[0] for c in state_calls]
        self.assertEqual(
            pattern,
            [build_frame([0x64]).hex().upper(), build_frame([0x68]).hex().upper()]
            * (ears.BURST_REPEATS + 1),
        )


class OffSemanticsTests(unittest.TestCase):
    def test_off_sent_once_and_never_refreshed_when_all_off(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x62))
        run(h.pair.turn_off_side("left"))
        self.assertFalse(h.pair.should_refresh)
        self.assertFalse(run(h.pair.refresh_tick()))
        off_sends = [c for c in h.calls
                     if bytes.fromhex(c[0])[:2] == b"\x90\x60"]
        self.assertEqual(len(off_sends), ears.BURST_REPEATS + 1)

    def test_mixed_pair_does_not_repeat(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.turn_off_side("right"))  # right already off; explicit
        run(h.pair.apply_simple("right", 0x60))
        self.assertFalse(h.pair.should_refresh)
        self.assertFalse(run(h.pair.refresh_tick()))

    def test_equal_pair_uses_canonical_single_byte_form(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x67))
        run(h.pair.apply_simple("right", 0x67))  # now equal -> 90 67
        frame = bytes.fromhex(h.calls[-1][0])
        self.assertEqual(len(frame), 3)
        self.assertEqual(frame[:2], b"\x90\x67")

    def test_both_off_burst_is_canonical_keepalive_form(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.turn_off_side("left"))       # both dark -> 90 60 A6
        frame = bytes.fromhex(h.calls[-1][0])
        self.assertEqual(bytes(frame), build_frame([0x60]))
        self.assertEqual(frame.hex().upper(), "9060A6")

    def test_refresh_reissues_current_canonical_form(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.apply_simple("right", 0x64))  # equal pair, refreshable
        before = len(h.calls)
        self.assertTrue(run(h.pair.refresh_tick()))
        frame = bytes.fromhex(h.calls[-1][0])
        self.assertEqual(len(h.calls), before + 1)
        self.assertEqual(frame[:2], b"\x90\x64")

    def test_no_state_lets_the_tick_emit_all_off(self):
        # Directive: there is NO all-off keep-alive. Sweep every reachable
        # state through many ticks; an all-off frame must never ride a tick.
        h = Harness()
        scenarios = [
            lambda: None,                                    # fresh/off
            lambda: run(h.pair.apply_palette(0x0E)),         # palette armed
            lambda: run(h.pair.apply_effect(0x84, "Strobe")),
            lambda: run(h.pair.apply_simple("left", 0x64)),
            lambda: run(h.pair.apply_simple("right", 0x61)),
            lambda: run(h.pair.turn_off_side("left")),
            lambda: run(h.pair.turn_off_side("right")),
        ]
        for scenario in scenarios:
            scenario()
            for _ in range(3):
                before = len(h.calls)
                run(h.pair.refresh_tick())
                for hex_frame, _rc in h.calls[before:]:
                    self.assertNotEqual(
                        bytes.fromhex(hex_frame)[:2], b"\x90\x60",
                        f"tick emitted all-off in state {h.pair.codes}",
                    )

    def test_palette_pick_does_not_arm_all_off_refresh(self):
        # Live-observed bug: picking a colour arms desired_on while codes
        # stay off; the 8 s tick then re-sent the 90 60 keep-alive forever,
        # killing every palette shade within seconds ("colours do nothing",
        # room ends dark).
        h = Harness()
        run(h.pair.apply_palette(0x09))
        before = len(h.calls)
        self.assertFalse(run(h.pair.refresh_tick()))
        self.assertFalse(h.pair.should_refresh)
        run(h.pair.refresh_tick())
        self.assertEqual(len(h.calls), before)

    def test_colour_writes_carry_leading_override_and_off_does_not(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x61))
        self.assertEqual(bytes.fromhex(h.calls[0][0])[1], 0x24)
        run(h.pair.apply_palette(0x03))
        idx = next(i for i, c in enumerate(h.calls)
                   if c[0].startswith("96"))
        self.assertEqual(bytes.fromhex(h.calls[idx - 1][0])[1], 0x24)
        before = len(h.calls)
        run(h.pair.turn_off_side("left"))
        for hex_frame, _rc in h.calls[before:]:
            self.assertNotEqual(bytes.fromhex(hex_frame)[1], 0x24)

    def test_refresh_requires_both_sides_actually_coloured(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        self.assertFalse(run(h.pair.refresh_tick()))   # right still off
        run(h.pair.apply_simple("right", 0x61))
        self.assertTrue(run(h.pair.refresh_tick()))

    def test_turning_off_already_dark_ear_sends_nothing(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        before = len(h.calls)
        run(h.pair.turn_off_side("right"))  # right was never lit
        self.assertEqual(len(h.calls), before)
        # and an all-dark pair stays silent too
        run(h.pair.turn_off_side("left"))
        # only the real all-off group burst (no override frame)
        self.assertEqual(len(h.calls), before + ears.BURST_REPEATS + 1)

    def test_fully_on_pair_repeats_once_per_tick(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.apply_simple("right", 0x66))
        before = len(h.calls)
        self.assertTrue(run(h.pair.refresh_tick()))
        # differing pair -> composed two-frame sequence, single pass
        self.assertEqual(len(h.calls), before + 2)
        _, repeats = h.calls[-1]
        self.assertEqual(repeats, 0)  # single shot on refresh
        self.assertEqual(bytes.fromhex(h.calls[-2][0]), build_frame([0x64]))
        self.assertEqual(bytes.fromhex(h.calls[-1][0]), build_frame([0x6E]))

    def test_refresh_resumes_after_full_pair_back_on(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.turn_off_side("right"))
        self.assertFalse(run(h.pair.refresh_tick()))
        run(h.pair.apply_simple("right", 0x63))
        self.assertTrue(h.pair.should_refresh)


class SuspensionTests(unittest.TestCase):
    def _foreign_frame(self):
        # A wand-style effect invoke we never sent.
        return build_frame([0x19, 0x11, 0x12, 0x04])

    def test_foreign_command_suspends(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        h.hub.ingest([self._foreign_frame()])
        self.assertIsNotNone(h.pair.suspended_by)
        self.assertIn("foreign command", h.pair.suspended_by)
        self.assertFalse(run(h.pair.refresh_tick()))

    def test_user_action_clears_suspension(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.apply_simple("right", 0x66))  # full pair -> refreshable
        h.hub.ingest([self._foreign_frame()])
        self.assertIsNotNone(h.pair.suspended_by)
        run(h.pair.apply_simple("left", 0x65))  # explicit user action
        self.assertIsNone(h.pair.suspended_by)
        self.assertTrue(run(h.pair.refresh_tick()))

    def test_own_echo_does_not_suspend(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        echo = bytes.fromhex(h.calls[1][0])  # first composed state frame
        h.hub.ingest([echo])
        self.assertIsNone(h.pair.suspended_by)

    def test_echo_stales_into_foreign_after_window(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        echo = bytes.fromhex(h.calls[1][0])  # first composed state frame
        h.now += ears.OURS_WINDOW_S + 1
        h.hub.ingest([echo])
        self.assertIsNotNone(h.pair.suspended_by)

    def test_beacon_updates_effect_only_never_suspends(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        beacon = build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])
        h.hub.ingest([beacon])
        self.assertIsNone(h.pair.suspended_by)
        self.assertEqual(
            h.hub.tracker.effect, "color sequence 0x88"
        )


class HubHarness(Harness):
    """Harness whose pair is registered with an ObservedHub."""

    def __init__(self):
        super().__init__()
        self.hub = ears.ObservedHub(clock=lambda: self.now)
        self.hub.pairs.append(self.pair)

    @property
    def now(self):  # shared mutable clock for pair + hub
        return self._now

    @now.setter
    def now(self, value):
        self._now = value


class ReceiverDataTests(unittest.TestCase):
    def test_bundle_parts_stored_separately(self):
        rd = ears.ReceiverData()
        phrase = build_frame([0x61, 0x6A])
        companion = build_frame([0x24, 0x58, 0xF0, 0x48, 0x04])
        rd.ingest([phrase, companion, phrase])
        self.assertTrue(rd.last_is_bundle)
        self.assertEqual(rd.last_phrase_hex, phrase.hex().upper())
        self.assertEqual(rd.last_companion_hex, companion.hex().upper())
        # non-bundle capture clears the stale companion
        single = build_frame([0x60])
        rd.ingest([single])
        self.assertFalse(rd.last_is_bundle)
        self.assertIsNone(rd.last_companion_hex)
        self.assertEqual(rd.last_phrase_hex, single.hex().upper())

    def test_counts_valid_and_invalid(self):
        rd = ears.ReceiverData()
        seen = []
        rd.listeners.append(lambda: seen.append(1))
        good = build_frame([0x60])
        bad = bytes.fromhex("906000")  # wrong CRC
        rd.ingest([good, bad])
        self.assertEqual(rd.message_count, 2)
        self.assertEqual(rd.invalid_count, 1)
        self.assertEqual(rd.last_frames_hex, good.hex().upper())
        self.assertEqual(len(seen), 1)  # only valid traffic notifies


if __name__ == "__main__":
    unittest.main()
