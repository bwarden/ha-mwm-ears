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
        self.pair = ears.EarPairState(self._transmit, clock=lambda: self.now)

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
        self.assertEqual(len(h.calls), 1)
        frame_hex, repeats = h.calls[0]
        self.assertEqual(repeats, ears.BURST_REPEATS)
        # Fused phrase carries left=red and right=off.
        frame = bytes.fromhex(frame_hex)
        self.assertEqual(frame[1], 0x64)
        self.assertEqual(frame[2], 0x60)

    def test_other_side_color_preserved_in_fused_frame(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x61))
        h.calls.clear()
        run(h.pair.apply_simple("right", 0x66))
        frame = bytes.fromhex(h.calls[0][0])
        self.assertEqual(frame[1], 0x61)  # left kept
        self.assertEqual(frame[2], 0x66)  # right updated

    def test_palette_template_frame_shape(self):
        h = Harness()
        run(h.pair.apply_palette(0x0E))
        frame = bytes.fromhex(h.calls[0][0])
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
        frame = bytes.fromhex(h.calls[-1][0])
        self.assertEqual(frame[1], 0x67)

    def test_restores_last_explicit_color(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))   # red
        run(h.pair.turn_off_side("left"))
        before = len(h.calls)
        sent = run(h.pair.turn_on_side("left"))
        self.assertEqual(sent, 0x64)
        frame = bytes.fromhex(h.calls[-1][0])
        self.assertEqual(frame[1], 0x64)
        self.assertEqual(len(h.calls), before + 1)  # bare on really transmits

    def test_sides_remember_independently(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.apply_simple("right", 0x61))
        self.assertEqual(run(h.pair.turn_on_side("right")), 0x61)
        # right's pick must not leak into left
        self.assertNotEqual(run(h.pair.turn_on_side("left")), 0x61)


class OffSemanticsTests(unittest.TestCase):
    def test_off_sent_once_and_never_refreshed_when_all_off(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x62))
        run(h.pair.turn_off_side("left"))
        self.assertFalse(h.pair.should_refresh)
        self.assertFalse(run(h.pair.refresh_tick()))
        off_sends = [c for c in h.calls
                     if bytes.fromhex(c[0])[:2] == b"\x90\x60"]
        self.assertEqual(len(off_sends), 1)

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
        self.assertEqual(len(h.calls), before + 1)  # only the real off burst

    def test_fully_on_pair_repeats_once_per_tick(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.apply_simple("right", 0x66))
        before = len(h.calls)
        self.assertTrue(run(h.pair.refresh_tick()))
        self.assertEqual(len(h.calls), before + 1)
        _, repeats = h.calls[-1]
        self.assertEqual(repeats, 0)  # single shot on refresh

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
        echo = build_frame([h.pair.codes["left"], h.pair.codes["right"]])
        h.hub.ingest([echo])
        self.assertIsNone(h.pair.suspended_by)

    def test_echo_stales_into_foreign_after_window(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        echo = build_frame([h.pair.codes["left"], h.pair.codes["right"]])
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
