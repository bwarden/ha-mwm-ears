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
    def test_initial_send_composes_twice_without_override(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        # No 24 override (rig 2026-08-23: canonical frames land without
        # it; the override blacks the ears for seconds). The composed
        # group runs BURST_REPEATS+1 = 2 passes.
        self.assertEqual(len(h.calls), 2 * (ears.BURST_REPEATS + 1))
        first, second = h.calls[0], h.calls[1]
        self.assertEqual(bytes.fromhex(first[0]), build_frame([0x64]))
        self.assertEqual(
            bytes.fromhex(second[0]), build_frame([0x68])
        )  # right-only OFF keeps left red
        for i, (_hexa, rc) in enumerate(h.calls):
            self.assertEqual(rc, 0)
            expected = first if i % 2 == 0 else second
            self.assertEqual(_hexa, expected[0])

    def test_right_only_change_sends_single_form(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x61))
        h.calls.clear()
        run(h.pair.apply_simple("right", 0x66))
        # Fast path: only the right slot changed -> single verified frame,
        # repeated; the left ear never sees an intermediate colour.
        self.assertEqual(
            h.calls,
            [(build_frame([0x6E]).hex().upper(), 0)] * (ears.BURST_REPEATS + 1),
        )

    def test_palette_pick_uses_short_tsv_form(self):
        h = Harness()
        run(h.pair.apply_palette(0x0E))
        frame = bytes.fromhex(h.calls[-1][0])
        # TSV short form: 91 0E pp crc
        self.assertEqual(len(frame), 4)
        self.assertEqual(frame[:3], bytes([0x91, 0x0E, 0x0E]))
        self.assertEqual(frame.hex().upper(), "910E0E40")

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
        group = h.calls[-(2 * (ears.BURST_REPEATS + 1)):]
        self.assertEqual(bytes.fromhex(group[0][0]), build_frame([0x67]))
        self.assertEqual(bytes.fromhex(group[1][0]), build_frame([0x68]))
        self.assertEqual(len(h.calls), 4)

    def test_restores_last_explicit_color(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))   # red
        run(h.pair.turn_off_side("left"))
        before = len(h.calls)
        sent = run(h.pair.turn_on_side("left"))
        self.assertEqual(sent, 0x64)
        # composed group x2 passes, leading with the colour; no override.
        self.assertEqual(len(h.calls),
                         before + 2 * (ears.BURST_REPEATS + 1))
        self.assertEqual(bytes.fromhex(h.calls[before][0]),
                         build_frame([0x64]))
        self.assertEqual(bytes.fromhex(h.calls[before + 1][0]),
                         build_frame([0x68]))
        self.assertEqual(bytes.fromhex(h.calls[-2][0]),
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
        pattern = [c[0] for c in h.calls]
        self.assertEqual(
            pattern,
            [build_frame([0x64]).hex().upper(), build_frame([0x68]).hex().upper()]
            * (ears.BURST_REPEATS + 1),
        )


class PairBothTests(unittest.TestCase):
    def test_apply_simple_both_sends_single_canonical_frame(self):
        h = Harness()
        run(h.pair.apply_simple_both(0x64))
        self.assertEqual(
            h.calls,
            [(build_frame([0x64]).hex().upper(), 0)]
            * (ears.BURST_REPEATS + 1),
        )
        self.assertEqual(h.pair.codes, {"left": 0x64, "right": 0x64})
        self.assertEqual(
            h.pair.last_simple, {"left": 0x64, "right": 0x64}
        )

    def test_turn_on_both_defaults_white_then_remembers(self):
        h = Harness()
        self.assertEqual(run(h.pair.turn_on_both()), 0x67)
        run(h.pair.apply_simple_both(0x62))
        run(h.pair.apply_simple_both(0x60))          # all off
        self.assertEqual(bytes.fromhex(h.calls[-1][0]), build_frame([0x60]))
        self.assertEqual(run(h.pair.turn_on_both()), 0x62)


class AdoptionTests(unittest.TestCase):
    """Foreign (wand) commands must move the lights, not just suspend."""

    def test_foreign_colour_command_adopted(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        h.hub.ingest([build_frame([0x62])])
        self.assertEqual(h.pair.codes, {"left": 0x62, "right": 0x62})
        self.assertIn("foreign command", h.pair.suspended_by)

    def test_own_echo_is_neither_adopted_nor_suspends(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        h.hub.ingest([bytes.fromhex(h.calls[0][0])])
        self.assertIsNone(h.pair.suspended_by)
        self.assertEqual(h.pair.codes["left"], 0x64)

    def test_foreign_right_only_touches_right_slot(self):
        h = HubHarness()
        run(h.pair.apply_simple_both(0x61))
        h.hub.ingest([build_frame([0x6E])])
        self.assertEqual(h.pair.codes, {"left": 0x61, "right": 0x66})

    def test_foreign_effect_invocation_adopted(self):
        from mwm.decode import effect_label

        h = HubHarness()
        h.hub.ingest([build_frame([0x24, 0x48, 0x84])])
        self.assertEqual(h.pair.running_effect, effect_label(0x84))
        self.assertTrue(all(h.pair.desired_on.values()))

    def test_beacon_updates_pair_effect_label_without_takeover(self):
        from mwm.decode import effect_label

        h = HubHarness()
        h.hub.ingest([build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])])
        self.assertEqual(h.pair.running_effect, effect_label(0x88))
        self.assertIsNone(h.pair.suspended_by)


class PaletteSideTests(unittest.TestCase):
    """Per-side palette via the TSV right-only template (91 0E pp|80)."""

    def test_frames_match_tsv_checksums(self):
        def frame(pp):
            return build_frame([0x0E, pp])

        # samples/mwm-gwts-colors.tsv rows:
        self.assertEqual(frame(0x00).hex().upper(), "910E005F")  # both
        self.assertEqual(frame(0x01).hex().upper(), "910E0101")
        self.assertEqual(frame(0x80).hex().upper(), "910E80D3")  # right
        self.assertEqual(frame(0x81).hex().upper(), "910E818D")

    def test_right_pick_is_single_right_only_frame(self):
        h = Harness()
        run(h.pair.apply_palette(0x00, side="right"))
        self.assertEqual(
            h.calls,
            [(build_frame([0x0E, 0x80]).hex().upper(), 0)]
            * (ears.BURST_REPEATS + 1),
        )
        self.assertEqual(h.pair.palette_code["right"], 0x00)
        self.assertIsNone(h.pair.palette_code["left"])

    def test_left_pick_composes_when_right_holds_palette(self):
        h = Harness()
        run(h.pair.apply_palette(0x00, side="right"))
        h.calls.clear()
        run(h.pair.apply_palette(0x04, side="left"))
        both = build_frame([0x0E, 0x04])
        ronly = build_frame([0x0E, 0x80])
        self.assertEqual(
            h.calls, [(both.hex().upper(), 0), (ronly.hex().upper(), 0),
                      (both.hex().upper(), 0), (ronly.hex().upper(), 0)]
        )
        self.assertEqual(h.pair.palette_code["left"], 0x04)
        self.assertEqual(h.pair.palette_code["right"], 0x00)

    def test_left_pick_degrades_to_both_without_right_palette(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        h.calls.clear()
        run(h.pair.apply_palette(0x09, side="left"))
        self.assertEqual(len(h.calls), ears.BURST_REPEATS + 1)
        self.assertEqual(h.pair.palette_code["left"], 0x09)
        self.assertEqual(h.pair.palette_code["right"], 0x09)

    def test_side_names_read_per_side(self):
        h = Harness()
        run(h.pair.apply_palette(0x00, side="right"))
        self.assertEqual(h.pair.side_color_name("right"), "sky")
        self.assertNotEqual(h.pair.side_color_name("left"), "sky")


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

    def test_effect_writes_carry_override_and_colour_writes_do_not(self):
        # The standalone `24` blacks the ears for seconds (flow control);
        # rig 2026-08-23 proved colour frames land without it, so only
        # effect invocation (doc: required to escape running programs)
        # still leads with it.
        h = Harness()
        run(h.pair.apply_simple("left", 0x61))
        self.assertEqual(bytes.fromhex(h.calls[0][0])[1], 0x61)
        run(h.pair.apply_palette(0x03))
        idx = next(i for i, c in enumerate(h.calls)
                   if c[0].startswith("91"))
        self.assertNotEqual(bytes.fromhex(h.calls[idx][0])[1], 0x24)
        before = len(h.calls)
        run(h.pair.turn_off_side("left"))
        for hex_frame, _rc in h.calls[before:]:
            self.assertNotEqual(bytes.fromhex(hex_frame)[1], 0x24)
        run(h.pair.apply_effect(0x84, "Strobe flash"))
        self.assertEqual(bytes.fromhex(h.calls[-1][0])[1], 0x24)

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
        echo = bytes.fromhex(h.calls[0][0])  # first composed state frame
        h.hub.ingest([echo])
        self.assertIsNone(h.pair.suspended_by)

    def test_echo_stales_into_foreign_after_window(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        echo = bytes.fromhex(h.calls[0][0])  # first composed state frame
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
