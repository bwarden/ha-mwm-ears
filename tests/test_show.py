"""Tests for .msh show parsing, scheduling, and replay (HA-free)."""

import asyncio
import importlib.util
import pathlib
import sys
import unittest

import _bootstrap  # noqa: F401  (must precede mwm imports)

from mwm import build_frame

# Load show.py without importing the integration package (its __init__
# pulls in homeassistant). Same trick as tests/test_ears.py.
_SHOW = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components" / "mwm_ears" / "show.py"
)
_spec = importlib.util.spec_from_file_location("show_core", _SHOW)
show = importlib.util.module_from_spec(_spec)
sys.modules["show_core"] = show
_spec.loader.exec_module(show)


def run(coro):
    return asyncio.run(coro)


class ParseTests(unittest.TestCase):
    def test_hex_lines_offsets_and_cumulative_time(self):
        beats = show.parse_show_script(
            "# a comment\n"
            "@100\n"
            "hex 9060A6\n"
            "hex 9060A6+9061A7\n"
            "@250\n"
            "hex 9062A8\n"
        )
        self.assertEqual([b["t_ms"] for b in beats], [100, 100, 250])
        self.assertEqual(
            [f.hex().upper() for b in beats for f in b["frames"]],
            ["9060A6", "9060A6", "9061A7", "9062A8"],
        )
        self.assertTrue(all(not b["cascade"] for b in beats))

    def test_same_line_offset(self):
        beats = show.parse_show_script("@500 hex 9060A6\n")
        self.assertEqual(beats[0]["t_ms"], 500)

    def test_unknown_verb_raises(self):
        with self.assertRaises(ValueError):
            show.parse_show_script("simple left blue\n")

    def test_dangling_offset_raises(self):
        with self.assertRaises(ValueError):
            show.parse_show_script("@100\n")

    def test_cue_countdown_pre_rolls_and_goes_last(self):
        beats = show.parse_show_script("cue hex F4 24 D1\n")
        beat = beats[0]
        self.assertTrue(beat["cascade"])
        self.assertEqual(beat["t_ms"], 0)
        ordered = [0xF4, 0xF3, 0xF2, 0xF1, 0x20]
        self.assertEqual(
            beat["frames"],
            [build_frame([d, 0x24, 0xD1]) for d in ordered],
        )
        self.assertEqual(beat["rel_ms"], [-400.0, -300.0, -200.0, -100.0, 0.0])

    def test_cascade_is_an_alias(self):
        cue = show.parse_show_script("cue hex F1 99\n")
        casc = show.parse_show_script("cascade hex F1 99\n")
        self.assertEqual(cue[0]["frames"], casc[0]["frames"])

    def test_members_clause_overrides_lead_set(self):
        beats = show.parse_show_script("cue hex F1 99 members FD 20\n")
        ordered = sorted([0xF1, 0xFD, 0x20], reverse=True)
        self.assertEqual(
            beats[0]["frames"], [build_frame([d, 0x99]) for d in ordered]
        )

    def test_plan_clamps_gap_and_keeps_cues_in_order(self):
        beats = show.parse_show_script("@1000 cue hex F4 24 D1\n")
        schedule = show.plan_show(beats, min_gap_ms=30, end_reset=False)
        self.assertEqual([t for t, _f in schedule], [600, 700, 800, 900, 1000])
        self.assertEqual(schedule[-1][1], build_frame([0x20, 0x24, 0xD1]))

    def test_plan_drops_preroll_that_cannot_fit(self):
        beats = show.parse_show_script("@50 cue hex F4 24 D1\n")
        schedule = show.plan_show(beats, min_gap_ms=30, end_reset=False)
        # No room to pre-roll: only the 20 GO survives, anchored on @ms.
        self.assertEqual([(t, f) for t, f in schedule],
                         [(50.0, build_frame([0x20, 0x24, 0xD1]))])

    def test_plan_appends_end_reset(self):
        beats = show.parse_show_script("@100 hex 9060A6\n")
        schedule = show.plan_show(beats, end_reset=True)
        self.assertEqual(schedule[-1], (130.0, show.END_RESET))
        self.assertEqual(show.END_RESET, build_frame([0x60]))


class _FakeLoop:
    """Deterministic async send/sleep/clock harness for ShowPlayer."""

    def __init__(self):
        self.t = 0.0
        self.sent: list[tuple[float, bytes]] = []

    def clock(self):
        return self.t

    async def sleep(self, delay):
        self.t += delay
        await asyncio.sleep(0)

    async def send(self, frame):
        self.sent.append((self.t, frame))


class PlayerTests(unittest.TestCase):
    def test_plays_schedule_at_planned_times(self):
        async def scenario():
            loop = _FakeLoop()
            player = show.ShowPlayer(
                loop.send, sleep=loop.sleep, clock=loop.clock, min_gap_ms=30
            )
            f1, f2 = build_frame([0x61]), build_frame([0x62])
            player.start([(0.0, f1), (1000.0, f2)], name="unit")
            self.assertTrue(player.playing)
            self.assertEqual(player.name, "unit")
            await player._task
            self.assertFalse(player.playing)
            return loop

        loop = run(scenario())
        self.assertEqual([(round(t, 3), f) for t, f in loop.sent],
                         [(0.0, build_frame([0x61])), (1.0, build_frame([0x62]))])

    def test_min_gap_floors_actual_sends(self):
        async def scenario():
            loop = _FakeLoop()
            player = show.ShowPlayer(
                loop.send, sleep=loop.sleep, clock=loop.clock, min_gap_ms=30
            )
            player.start([(0.0, build_frame([0x61])), (0.0, build_frame([0x62]))])
            await player._task
            return loop

        loop = run(scenario())
        self.assertEqual([round(t, 3) for t, _f in loop.sent], [0.0, 0.03])

    def test_stop_cancels_playback(self):
        async def scenario():
            loop = _FakeLoop()
            player = show.ShowPlayer(
                loop.send, sleep=loop.sleep, clock=loop.clock
            )
            player.start([(0.0, build_frame([0x61])), (5000.0, build_frame([0x62]))])
            await asyncio.sleep(0)
            task = player._task
            player.stop()
            self.assertFalse(player.playing)
            with self.assertRaises(asyncio.CancelledError):
                await task

        run(scenario())

    def test_repeat_replays_with_gap(self):
        async def scenario():
            loop = _FakeLoop()
            player = show.ShowPlayer(
                loop.send, sleep=loop.sleep, clock=loop.clock
            )
            f1 = build_frame([0x61])
            player.start([(0.0, f1), (100.0, f1)], repeat=2, gap_s=1.0)
            await player._task
            return loop

        loop = run(scenario())
        self.assertEqual([round(t, 3) for t, _f in loop.sent],
                         [0.0, 0.1, 1.1, 1.2])

    def test_seconds_of(self):
        self.assertEqual(show.seconds_of([(0.0, b""), (2500.0, b"")]), 2.5)


if __name__ == "__main__":
    unittest.main()
