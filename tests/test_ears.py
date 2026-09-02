"""Tests for the HA-free ear-pair state logic (off/suspend/adoption rules)."""

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
    def test_initial_send_composes_fused_frame(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        # Fused frame: 91 64 68 = left red, right off in one burst.
        # The composed group runs BURST_REPEATS+1 = 2 passes.
        self.assertEqual(len(h.calls), 1 * (ears.BURST_REPEATS + 1))
        fused = build_frame([0x64, ears.RIGHT_ONLY_BASE])
        for i, (_hexa, rc) in enumerate(h.calls):
            self.assertEqual(rc, 0)
            self.assertEqual(bytes.fromhex(_hexa), fused)

    def test_right_only_change_sends_single_form(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x61))
        h.calls.clear()
        run(h.pair.apply_simple("right", 0x66))
        # Fast path: only the right slot changed -> single verified frame,
        # repeated; the left ear never sees an intermediate color.
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

    def test_effect_invoke_starts_with_current_color(self):
        h = Harness()
        run(h.pair.apply_effect(0x84, "Strobe flash"))
        # Effect frame first, WITHOUT the 24 reset (which blanks the ears),
        # then the current pair color re-issued so the effect adopts it.
        effect = bytes.fromhex(h.calls[0][0])
        self.assertEqual(list(effect[1:-1]), [0x48, 0x84])
        # A fresh pair defaults to both-ears-off (0x60), sent right after.
        color = bytes.fromhex(h.calls[1][0])
        self.assertEqual(list(color[1:-1]), [0x60])

    def test_effect_invoke_seeds_explicit_color(self):
        h = Harness()
        run(h.pair.apply_effect(
            0x04, "Pulse",
            left=("simple", 0x65), right=("simple", 0x65),
        ))
        # Pulse needs its 58 F0 cycle-timer companion.
        effect = bytes.fromhex(h.calls[0][0])
        self.assertEqual(list(effect[1:-1]), [0x48, 0x04, 0x58, 0xF0])
        # Explicit seed color (both ears) sent right after the effect.
        color = bytes.fromhex(h.calls[1][0])
        self.assertEqual(list(color[1:-1]), [0x65])
        # And recorded so HA reflects it.
        self.assertEqual(h.pair.codes["left"], 0x65)
        self.assertEqual(h.pair.codes["right"], 0x65)


class TurnOnRestoreTests(unittest.TestCase):
    def test_fresh_pair_defaults_to_white(self):
        h = Harness()
        sent = run(h.pair.turn_on_side("left"))
        self.assertEqual(sent, 0x67)
        group = h.calls[-(1 * (ears.BURST_REPEATS + 1)):]
        fused = build_frame([0x67, ears.RIGHT_ONLY_BASE])  # left white, right off
        self.assertEqual(bytes.fromhex(group[0][0]), fused)
        self.assertEqual(len(h.calls), 2)

    def test_restores_last_explicit_color(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))   # red
        run(h.pair.turn_off_side("left"))
        before = len(h.calls)
        sent = run(h.pair.turn_on_side("left"))
        self.assertEqual(sent, 0x64)
        # Fused group x2 passes: single frame with left red, right off.
        self.assertEqual(len(h.calls),
                         before + 1 * (ears.BURST_REPEATS + 1))
        fused = build_frame([0x64, ears.RIGHT_ONLY_BASE])
        self.assertEqual(bytes.fromhex(h.calls[before][0]), fused)
        self.assertEqual(bytes.fromhex(h.calls[-1][0]), fused)

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

    def test_mixed_pair_composes_fused_frame(self):
        RIGHT_ONLY_BASE, EAR_OFF_CODE = ears.RIGHT_ONLY_BASE, ears.EAR_OFF_CODE
        from ears_core import LEFT, RIGHT
        h = Harness()
        h.pair.codes = {LEFT: 0x62, RIGHT: 0x66}
        frames = h.pair._state_frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], build_frame([0x62, RIGHT_ONLY_BASE + 0x66 - EAR_OFF_CODE]))

    def test_left_dark_right_lit_uses_fused_frame(self):
        from ears_core import LEFT, RIGHT
        h = Harness()
        h.pair.codes = {LEFT: 0x60, RIGHT: 0x63}
        frames = h.pair._state_frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], build_frame([0x60, 0x6B]))

    def test_group_repeats_preserve_fused_frame(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        pattern = [c[0] for c in h.calls]
        fused_hex = build_frame([0x64, ears.RIGHT_ONLY_BASE]).hex().upper()
        self.assertEqual(
            pattern,
            [fused_hex] * (ears.BURST_REPEATS + 1),
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

    def test_beacon_updates_pair_active_state(self):
        """Beacons indicate the ears are active — the pair bound to the
        hearing receiver should reflect desired_on and running_effect."""
        from mwm.decode import effect_label

        h = HubHarness()
        h.hub.ingest([build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])])
        self.assertEqual(h.pair.running_effect, effect_label(0x88))
        self.assertTrue(all(h.pair.desired_on.values()))
        self.assertIsNone(h.pair.suspended_by)
        self.assertGreater(h.hub.last_beacon_at, 0)

    def test_stale_beacon_within_window_does_not_relight_after_off(self):
        """OFF must stick: a beacon heard right after our own command was in
        flight when the ears processed the off, so it must not set
        desired_on back to True (was slide-back bug: the off stuck on the
        hardware but the next overheard beacon re-lit the entity)."""
        h = HubHarness()
        run(h.pair.apply_simple_both(ears.EAR_OFF_CODE))
        self.assertFalse(any(h.pair.desired_on.values()))
        # A stale beacon arrives 2 s later — inside the debounce window.
        h.now += 2.0
        h.hub.ingest(
            [build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])]
        )
        self.assertFalse(any(h.pair.desired_on.values()))

    def test_beacon_after_stale_window_relights(self):
        """Once the debounce window passes, a live beacon correctly shows
        the ears as on again (they genuinely kept/woke beaconing)."""
        h = HubHarness()
        run(h.pair.apply_simple_both(ears.EAR_OFF_CODE))
        self.assertFalse(any(h.pair.desired_on.values()))
        h.now += ears.BEACON_STALE_AFTER_COMMAND_S + 1.0
        h.hub.ingest(
            [build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])]
        )
        self.assertTrue(all(h.pair.desired_on.values()))


class EnforcementTests(unittest.TestCase):
    """Enforce-ears switch mode: always re-assert; assume-off is passive."""

    def test_enforce_tick_reasserts_held_state_single_pass(self):
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_tick())
        # Re-assert sends the held state once (no extra repeats): 90 64.
        self.assertEqual(h.calls, [(build_frame([0x64]).hex().upper(), 0)])

    def test_enforce_tick_sends_nothing_when_passive(self):
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))
        self.assertFalse(h.pair.enforce)  # switch defaults OFF
        h.calls.clear()
        run(h.pair.enforce_tick())
        self.assertEqual(h.calls, [])

    def test_enforce_tick_reasserts_off_when_enforcing_off(self):
        # Enforcing an OFF target still re-asserts (keeps ears dark against
        # demo mode) -- it does NOT go quiet.
        h = HubHarness()
        run(h.pair.apply_simple_both(ears.EAR_OFF_CODE))
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_tick())
        self.assertEqual(h.calls, [(build_frame([0x60]).hex().upper(), 0)])

    def test_enforce_tick_backs_off_when_off_target(self):
        # OFF target: re-asserts, then backs off to ENFORCE_OFF_INTERVAL_S.
        h = HubHarness()
        run(h.pair.apply_simple_both(ears.EAR_OFF_CODE))
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_tick())  # first tick always sends
        self.assertEqual(len(h.calls), 1)
        # A second tick well inside the back-off window sends nothing.
        h.now += 20.0
        run(h.pair.enforce_tick())
        self.assertEqual(len(h.calls), 1)
        # Past the back-off window it re-asserts again.
        h.now += ears.ENFORCE_OFF_INTERVAL_S
        run(h.pair.enforce_tick())
        self.assertEqual(len(h.calls), 2)

    def test_enforce_reassert_ignores_backoff_and_restarts_it(self):
        h = HubHarness()
        run(h.pair.apply_simple_both(ears.EAR_OFF_CODE))
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_reassert())  # immediate, always sends
        self.assertEqual(len(h.calls), 1)
        # It restarted the timer: a tick just inside the window sends nothing.
        h.now += 30.0
        run(h.pair.enforce_tick())
        self.assertEqual(len(h.calls), 1)

    def test_enforce_on_target_reasserts_on_every_tick(self):
        # ON target: re-asserts on every fast tick, no back-off.
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_tick())
        h.now += ears.ENFORCE_INTERVAL_S
        run(h.pair.enforce_tick())
        self.assertEqual(len(h.calls), 2)

    def test_assume_off_passive_when_silent(self):
        # Passive + light ON + beacon silence past the timeout -> assume off.
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))
        h.now += 1.0
        h.hub.ingest([build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])])
        self.assertFalse(h.pair.assume_off_if_silent(45.0))
        h.now += 45.0 + 1.0
        self.assertTrue(h.pair.assume_off_if_silent(45.0))
        self.assertFalse(any(h.pair.desired_on.values()))
        self.assertIsNone(h.pair.running_effect)

    def test_assume_off_when_never_beamed(self):
        # A room that is turned ON but never beacons (ears dead or carried
        # away) must still be assumed off after the timeout.  The old guard
        # on `_last_beacon_at == 0` left such a pair stuck ON forever.
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))  # turn on, no beacon ever arrives
        self.assertFalse(h.pair.assume_off_if_silent(45.0))
        h.now += 45.0 + 1.0
        self.assertTrue(h.pair.assume_off_if_silent(45.0))
        self.assertFalse(any(h.pair.desired_on.values()))

    def test_assume_off_never_while_enforcing(self):
        # Enforcing never assumes off -- even after beacon silence.
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))
        h.pair.set_enforce(True)
        h.now += 1.0
        h.hub.ingest([build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])])
        h.now += 45.0 + 10.0
        self.assertFalse(h.pair.assume_off_if_silent(45.0))
        self.assertTrue(all(h.pair.desired_on.values()))

    def test_assume_off_noop_when_light_off(self):
        # Passive + light already OFF: the rule is only about ON states.
        h = HubHarness()
        run(h.pair.apply_simple_both(ears.EAR_OFF_CODE))
        self.assertFalse(h.pair.assume_off_if_silent(45.0))

    def test_foreign_command_overridden_while_enforcing(self):
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))  # red
        h.pair.set_enforce(True)
        # A foreign blue command arrives while we enforce.
        h.now += 1.0
        reassert = h.hub.ingest([build_frame([0x61])])
        self.assertEqual(h.pair.codes, {ears.LEFT: 0x64, ears.RIGHT: 0x64})
        self.assertIsNone(h.pair.suspended_by)
        self.assertTrue(all(h.pair.desired_on.values()))
        # The enforcing pair is requested for an immediate re-assert.
        self.assertIn(h.pair, reassert)
        # Non-enforcing pair adopts the same foreign command, no re-assert.
        h2 = HubHarness()
        run(h2.pair.apply_simple_both(0x64))
        h2.now += 1.0
        reassert2 = h2.hub.ingest([build_frame([0x61])])
        self.assertEqual(h2.pair.codes, {ears.LEFT: 0x61, ears.RIGHT: 0x61})
        self.assertIsNotNone(h2.pair.suspended_by)
        self.assertNotIn(h2.pair, reassert2)

    def test_beacon_triggers_immediate_reassert(self):
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))  # red held
        h.pair.set_enforce(True)
        # We never transmit beacons, so any beacon heard is foreign ->
        # while enforcing we pull back onto our held state immediately.
        beacon = build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])
        self.assertIn(h.pair, h.hub.ingest([beacon]))

    def test_own_echo_does_not_trigger_reassert(self):
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))  # red held, frame remembered
        h.pair.set_enforce(True)
        # Our own transmitted frame bouncing back is "ours" (matches_recent)
        # -> not foreign, so no immediate re-assert (no self-sustaining loop).
        echo = build_frame([0x64])
        self.assertNotIn(h.pair, h.hub.ingest([echo]))

    def test_enforce_reasserts_exact_held_palette_frames(self):
        """Palette shades cannot be re-expressed via simple codes; the
        enforcement re-assert must replay the exact held frames of the last
        explicit pick (91 0E pp), not a re-derived 90 6X state."""
        h = HubHarness()
        run(h.pair.apply_palette(0x03))  # both ears to palette shade 0x03
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_tick())
        self.assertEqual(
            h.calls,
            [(build_frame([0x0E, 0x03]).hex().upper(), 0)],
        )

    def test_enforce_reasserts_held_effect_frames(self):
        """Effect programs are held and re-asserted frame-for-frame too:
        effect + current-color replay, not a simple color."""
        h = HubHarness()
        run(h.pair.apply_effect(0x84, "Strobe flash"))
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_tick())
        # _hold_frames = [effect_frame, color_frame] (no 24 prefix);
        # both replay via _send_group, one per frame.
        self.assertEqual(
            h.calls,
            [
                (build_frame([0x48, 0x84]).hex().upper(), 0),
                (build_frame([0x60]).hex().upper(), 0),
            ],
        )

    def test_enforce_fresh_restart_defaults_to_off(self):
        """A pair that (re)started while enforcing and has never received an
        explicit command holds all-off -- never a re-derived transient."""
        h = HubHarness()
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_tick())
        self.assertEqual(h.calls, [(build_frame([0x60]).hex().upper(), 0)])

    def test_enable_enforce_after_foreign_adoption_holds_adopted(self):
        """Turning enforcement ON after a foreign command was adopted must
        re-anchor the hold to the ADOPTED display state, not re-blast the
        stale pre-wand user command (was: switch-on raced the two and the
        first re-assert overrode what the room was actually showing)."""
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))   # user: red
        h.hub.ingest([build_frame([0x62])])   # foreign blue adopted
        self.assertEqual(h.pair.codes, {ears.LEFT: 0x62, ears.RIGHT: 0x62})
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_tick())
        # It holds the adopted blue, NOT the pre-wand red.
        self.assertEqual(h.calls, [(build_frame([0x62]).hex().upper(), 0)])

    def test_enable_enforce_after_wand_adoption_holds_phrase(self):
        """A foreign wand program drives a running_effect we cannot recompose
        from codes; enabling enforcement must hold the captured phrase bytes
        itself so the re-assert faithfully replays the foreign state."""
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))   # user: red
        phrase = bytes.fromhex("9619102410D24E03")  # pulsating red wand program
        h.hub.ingest([build_frame(phrase)])
        self.assertIsNotNone(h.pair.running_effect)
        h.pair.set_enforce(True)
        h.calls.clear()
        run(h.pair.enforce_reassert())        # immediate re-assert, no back-off
        self.assertEqual(h.calls, [(build_frame(phrase).hex().upper(), 0)])

    def test_enforcing_pair_never_clobbers_hold(self):
        """While already enforcing, foreign frames are overridden (not
        adopted) and must not move `_hold_frames` -- the held red stays the
        red frames even after a foreign blue arrives beneath the re-assert."""
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))  # red held
        h.pair.set_enforce(True)
        h.now += 1.0
        h.hub.ingest([build_frame([0x61])])  # foreign blue, overridden
        self.assertIn(h.pair, h.hub.ingest([build_frame([0x61])]))
        self.assertEqual(h.pair._hold_frames, [build_frame([0x64])])

    def test_enforcing_off_beacon_does_not_relight_display(self):
        """Enforcing an OFF target: a live (non-stale) beacon proves the ears
        are physically on -- exactly the demo-mode aftermath of a power-on --
        but enforcement is sole authority: the entity must keep showing our
        held OFF even though the beacon still triggers the immediate re-
        assert that keeps the hardware dark."""
        h = HubHarness()
        run(h.pair.apply_simple_both(ears.EAR_OFF_CODE))
        h.pair.set_enforce(True)
        # Advance past the stale window so the beacon is GENUINELY live:
        # that is the case that used to slide the enforcing-OFF display on.
        h.now += ears.BEACON_STALE_AFTER_COMMAND_S + 1.0
        beacon = build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])
        reassert = h.hub.ingest([beacon])
        # The beacon still pulls the ears back onto our held OFF...
        self.assertIn(h.pair, reassert)
        # ...but the display must not slide back ON, and the beacon's demo
        # effect must not clobber the held (all-off) display either.
        self.assertFalse(any(h.pair.desired_on.values()))
        self.assertIsNone(h.pair.running_effect)

    def test_enforcing_on_beacon_keeps_held_on(self):
        """Enforcing an ON target, a beacon only confirms the held-live
        state -- desired_on stays as commanded (no accidental flip off)."""
        h = HubHarness()
        run(h.pair.apply_simple_both(0x64))  # red held
        h.pair.set_enforce(True)
        h.now += ears.BEACON_STALE_AFTER_COMMAND_S + 1.0
        beacon = build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])
        self.assertIn(h.pair, h.hub.ingest([beacon]))
        self.assertTrue(all(h.pair.desired_on.values()))
        # Passive peers still adopt the beacon's effect into the display.
        h2 = HubHarness()
        run(h2.pair.apply_simple_both(0x64))
        h2.now += ears.BEACON_STALE_AFTER_COMMAND_S + 1.0
        h2.hub.ingest([beacon])
        self.assertIsNotNone(h2.pair.running_effect)


class CrossRoomIsolationTests(unittest.TestCase):
    """Foreign commands on one receiver must not mutate pairs in other rooms."""

    def test_foreign_command_scoped_to_receiver(self):
        hub = ears.ObservedHub()
        # Two pairs in different rooms, each bound to a different receiver.
        pair_a = ears.EarPairState(
            lambda f, rc=0: None, repeat_gap_s=0,
        )
        pair_a.receiver_entity = "infrared.receiver_a"
        pair_b = ears.EarPairState(
            lambda f, rc=0: None, repeat_gap_s=0,
        )
        pair_b.receiver_entity = "infrared.receiver_b"
        hub.pairs = [pair_a, pair_b]

        # A foreign wand command arrives on receiver_a only.
        phrase = bytes.fromhex("9619102410D24E03")  # pulsating red
        hub.ingest([build_frame(phrase)], receiver="infrared.receiver_a")

        # pair_a should be adopted; pair_b untouched.
        self.assertEqual(pair_a.running_effect, "wand program: pulsating red")
        self.assertTrue(all(pair_a.desired_on.values()))
        self.assertIsNone(pair_b.running_effect)
        self.assertFalse(any(pair_b.desired_on.values()))

    def test_beacon_scoped_to_receiver(self):
        """Beacon on one receiver only updates the pair bound to that
        receiver, not pairs in other rooms."""
        from mwm.decode import effect_label

        hub = ears.ObservedHub()
        pair_a = ears.EarPairState(
            lambda f, rc=0: None, repeat_gap_s=0,
        )
        pair_a.receiver_entity = "infrared.receiver_a"
        pair_b = ears.EarPairState(
            lambda f, rc=0: None, repeat_gap_s=0,
        )
        pair_b.receiver_entity = "infrared.receiver_b"
        hub.pairs = [pair_a, pair_b]

        hub.ingest(
            [build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40])],
            receiver="infrared.receiver_a",
        )
        # pair_a should reflect active ears; pair_b untouched.
        self.assertEqual(pair_a.running_effect, effect_label(0x88))
        self.assertTrue(all(pair_a.desired_on.values()))
        self.assertIsNone(pair_b.running_effect)
        self.assertFalse(any(pair_b.desired_on.values()))

    def test_receiver_none_updates_all_pairs(self):
        hub = ears.ObservedHub()
        pair_a = ears.EarPairState(
            lambda f, rc=0: None, repeat_gap_s=0,
        )
        pair_b = ears.EarPairState(
            lambda f, rc=0: None, repeat_gap_s=0,
        )
        hub.pairs = [pair_a, pair_b]

        phrase = bytes.fromhex("9619102410D24E03")
        hub.ingest([build_frame(phrase)])  # no receiver specified
        self.assertTrue(all(pair_a.desired_on.values()))
        self.assertTrue(all(pair_b.desired_on.values()))


class WandCommandTests(unittest.TestCase):
    """96 19 wand phrases decode to programs/colors and drive adoption."""

    def test_documented_program_adopted_as_running_effect(self):
        h = HubHarness()
        phrase = bytes.fromhex("9619102410D24E03")   # docs: pulsating red
        h.hub.ingest([build_frame(phrase)])
        self.assertEqual(h.pair.running_effect, "wand program: pulsating red")
        self.assertTrue(all(h.pair.desired_on.values()))
        self.assertIn("foreign command", h.pair.suspended_by)

    def test_unknown_program_still_records(self):
        h = HubHarness()
        phrase = bytes.fromhex("9619102D0A2A9C03")   # live rig capture
        h.hub.ingest([build_frame(phrase)])
        self.assertIn("wand program 10/2D", h.pair.running_effect)
        self.assertIsNone(h.pair.palette_code["left"])

    def test_static_colour_phrase_sets_palette_slots(self):
        from ears_core import EAR_OFF_CODE, LEFT

        h = HubHarness()
        phrase = bytes.fromhex("9619070F16A21804")   # docs static template
        h.hub.ingest([build_frame(phrase)])
        self.assertEqual(h.pair.palette_code[LEFT], 0xA2 & 0x7F)
        self.assertEqual(h.pair.codes[LEFT], EAR_OFF_CODE)


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
        # LEFT palette pick preserves the right's palette shade by fusing
        # the pair into ONE frame: left 0x04, right 0x00 (per-ear register).
        fused = build_frame([0x0E, 0x04, 0x0E, 0x80])
        self.assertEqual(fused.hex().upper(), "930E040E8022")
        self.assertEqual(
            h.calls, [(fused.hex().upper(), 0)] * (ears.BURST_REPEATS + 1)
        )
        self.assertEqual(h.pair.palette_code["left"], 0x04)
        self.assertEqual(h.pair.palette_code["right"], 0x00)

    def test_left_pick_preserves_simple_right_in_fused_frame(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))   # right still simple off
        run(h.pair.apply_simple("right", 0x61))  # right holds simple blue
        h.calls.clear()
        run(h.pair.apply_palette(0x09, side="left"))
        # LEFT palette pick must NOT clobber the simple right: fuse the pair
        # into ONE frame (left palette 0x09, right simple blue via 0x68+..).
        fused = build_frame([0x0E, 0x09, ears.RIGHT_ONLY_BASE + 0x61 - ears.EAR_OFF_CODE])
        self.assertEqual(fused.hex().upper(), "920E096959")
        self.assertEqual(
            h.calls, [(fused.hex().upper(), 0)] * (ears.BURST_REPEATS + 1)
        )
        self.assertEqual(h.pair.palette_code["left"], 0x09)
        self.assertIsNone(h.pair.palette_code["right"])
        self.assertEqual(h.pair.codes["right"], 0x61)

    def test_left_simple_pick_preserves_right_palette_in_fused_frame(self):
        h = Harness()
        run(h.pair.apply_palette(0x00, side="right"))
        h.calls.clear()
        run(h.pair.apply_simple("left", 0x64))
        # Changing the LEFT simple color must leave the right's palette
        # shade untouched -- fused `92 64 0E 80` in one burst.
        fused = build_frame([0x64, 0x0E, 0x00 | 0x80])
        self.assertEqual(
            h.calls, [(fused.hex().upper(), 0)] * (ears.BURST_REPEATS + 1)
        )
        self.assertEqual(h.pair.palette_code["right"], 0x00)
        self.assertEqual(h.pair.codes["left"], 0x64)

    def test_left_off_preserves_right_palette_in_fused_frame(self):
        h = Harness()
        run(h.pair.apply_palette(0x00, side="right"))
        run(h.pair.apply_simple("left", 0x64))
        h.calls.clear()
        run(h.pair.turn_off_side("left"))
        # Turning LEFT off (via both-ear op) tacks on a right-only palette
        # restore so the right stays lit -- no clobber to off.
        fused = build_frame([0x60, 0x0E, 0x00 | 0x80])
        self.assertEqual(
            h.calls, [(fused.hex().upper(), 0)] * (ears.BURST_REPEATS + 1)
        )
        self.assertEqual(h.pair.palette_code["right"], 0x00)
        self.assertEqual(h.pair.codes["left"], 0x60)

    def test_side_names_read_per_side(self):
        h = Harness()
        run(h.pair.apply_palette(0x00, side="right"))
        self.assertEqual(h.pair.side_color_name("right"), "pale cyan-white")
        self.assertNotEqual(h.pair.side_color_name("left"), "pale cyan-white")


class OffSemanticsTests(unittest.TestCase):
    def test_off_sent_once_in_burst(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x62))
        run(h.pair.turn_off_side("left"))
        off_sends = [c for c in h.calls
                     if bytes.fromhex(c[0])[:2] == b"\x90\x60"]
        self.assertEqual(len(off_sends), ears.BURST_REPEATS + 1)

    def test_mixed_pair_composes_correctly(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x64))
        h.calls.clear()
        run(h.pair.apply_simple("right", 0x60))
        # right goes dark via right-only form; left untouched
        self.assertEqual(len(h.calls), ears.BURST_REPEATS + 1)
        self.assertEqual(
            h.calls[0][0], build_frame([ears.RIGHT_ONLY_BASE]).hex().upper()
        )

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

    def test_user_action_clears_suspension(self):
        h = HubHarness()
        run(h.pair.apply_simple("left", 0x64))
        run(h.pair.apply_simple("right", 0x66))  # full pair
        h.hub.ingest([self._foreign_frame()])
        self.assertIsNotNone(h.pair.suspended_by)
        run(h.pair.apply_simple("left", 0x65))  # explicit user action
        self.assertIsNone(h.pair.suspended_by)

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

    def test_note_signal_notifies_listeners(self):
        rd = ears.ReceiverData()
        seen = []
        rd.listeners.append(lambda: seen.append(rd.signals_seen))
        # note_signal increments signals_seen and notifies listeners
        rd.note_signal("dummy", [[417, -417]])
        self.assertEqual(rd.signals_seen, 1)
        self.assertEqual(seen, [1])
        rd.note_signal("dummy", [[834, -417]])
        self.assertEqual(rd.signals_seen, 2)
        self.assertEqual(seen, [1, 2])

    def test_note_signal_updates_debug_even_without_frames(self):
        rd = ears.ReceiverData()
        rd.note_signal("fake", [])
        self.assertEqual(rd.signals_seen, 1)
        self.assertIsNotNone(rd.last_signal_debug)
        self.assertEqual(rd.last_signal_debug["candidate_lengths"], [])


class SidePickedTests(unittest.TestCase):
    """Color identity mirrors the store: exact commands and wheel snaps both
    write codes/palette indices, so side_picked is the single, RGB-free
    source that lets the card highlight distinct near-identical shades."""

    def test_simple_pick_identity(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x62))
        self.assertEqual(h.pair.side_picked("left"), {"kind": "simple", "code": 0x62})

    def test_palette_pick_identity(self):
        h = Harness()
        run(h.pair.apply_palette(0x0E, side="right"))
        self.assertEqual(h.pair.side_picked("right"), {"kind": "palette", "index": 0x0E})

    def test_both_palette_agrees(self):
        h = Harness()
        run(h.pair.apply_palette(0x04))
        self.assertEqual(h.pair.side_picked("both"), {"kind": "palette", "index": 0x04})

    def test_both_diverges_to_none(self):
        h = Harness()
        run(h.pair.apply_simple("left", 0x62))
        run(h.pair.apply_palette(0x0E, side="right"))
        self.assertIsNone(h.pair.side_picked("both"))
        # Side queries still answer their own identity.
        self.assertEqual(h.pair.side_picked("left"), {"kind": "simple", "code": 0x62})
        self.assertEqual(h.pair.side_picked("right"), {"kind": "palette", "index": 0x0E})

    def test_off_and_effect_yield_none(self):
        h = Harness()
        run(h.pair.apply_simple("left", ears.EAR_OFF_CODE))
        self.assertIsNone(h.pair.side_picked("left"))
        run(h.pair.apply_effect(0x84, "Strobe flash"))
        self.assertIsNone(h.pair.side_picked("both"))
        self.assertIsNone(h.pair.side_picked("left"))


class ApplyStateTests(unittest.TestCase):
    """mwm_ears.set_state (apply_state) pair-level control."""

    def setUp(self):
        self.h = Harness()

    def _set(self, left_code, right_code):
        p = self.h.pair
        p.codes = {"left": left_code, "right": right_code}
        p.desired_on = {
            "left": left_code != ears.EAR_OFF_CODE,
            "right": right_code != ears.EAR_OFF_CODE,
        }
        p.palette_code = {"left": None, "right": None}

    def test_noop_when_nothing_set(self):
        run(self.h.pair.apply_state())
        self.assertEqual(self.h.calls, [])

    def test_both_shortcut_uses_native_both_form(self):
        p = self.h.pair
        run(p.apply_state(both=("simple", 0x62)))
        self.assertEqual(p.codes, {"left": 0x62, "right": 0x62})
        self.assertTrue(p.desired_on["left"] and p.desired_on["right"])
        self.assertIsNone(p.palette_code["left"])
        # Single composed group, not two per-side bursts.
        self.assertEqual(len(self.h.calls), ears.BURST_REPEATS + 1)

    def test_both_off_collapses_to_single_command(self):
        p = self.h.pair
        self._set(0x64, 0x67)
        run(p.apply_state(both=("simple", ears.EAR_OFF_CODE)))
        self.assertFalse(p.desired_on["left"] or p.desired_on["right"])
        self.assertEqual(p.codes, {"left": ears.EAR_OFF_CODE, "right": ears.EAR_OFF_CODE})

    def test_side_override_wins_over_color_shortcut(self):
        p = self.h.pair
        run(p.apply_state(both=("simple", 0x62), left=("simple", 0x66)))
        self.assertEqual(p.codes["left"], 0x66)
        self.assertEqual(p.codes["right"], 0x62)

    def test_per_side_left_leaves_right_untouched(self):
        p = self.h.pair
        self._set(0x64, 0x67)
        run(p.apply_state(left=("simple", 0x66)))
        self.assertEqual(p.codes["left"], 0x66)
        self.assertEqual(p.codes["right"], 0x67)
        self.assertEqual(p.palette_code, {"left": None, "right": None})

    def test_identical_separate_picks_use_one_native_frame(self):
        p = self.h.pair
        self._set(ears.EAR_OFF_CODE, ears.EAR_OFF_CODE)
        run(p.apply_state(left=("palette", 4), right=("palette", 4)))
        self.assertEqual(len(self.h.calls), ears.BURST_REPEATS + 1)
        self.assertEqual(p.palette_code["left"], 4)
        self.assertEqual(p.palette_code["right"], 4)

    def test_mixed_picks_apply_both_sides(self):
        p = self.h.pair
        self._set(0x64, 0x64)
        run(p.apply_state(left=("simple", 0x66), right=("palette", 9)))
        self.assertEqual(p.codes["left"], 0x66)
        self.assertIsNone(p.palette_code["left"])
        self.assertEqual(p.palette_code["right"], 9)
        # A right-only palette pick tracks palette_code, not codes (which
        # still holds the last simple shade the right ear was on).
        self.assertEqual(p.codes["right"], 0x64)

    def test_off_already_off_ear_is_noop(self):
        p = self.h.pair
        self._set(0x67, ears.EAR_OFF_CODE)
        run(p.apply_state(right=("simple", ears.EAR_OFF_CODE)))
        self.assertEqual(self.h.calls, [])
        self.assertEqual(p.codes["right"], ears.EAR_OFF_CODE)

    def test_effect_applied_after_colors(self):
        p = self.h.pair
        run(p.apply_state(both=("simple", 0x62), effect=(0x84, "Strobe flash")))
        self.assertEqual(p.codes["left"], 0x62)
        self.assertEqual(p.running_effect, "Strobe flash")
        self.assertTrue(p.desired_on["left"] and p.desired_on["right"])


if __name__ == "__main__":
    unittest.main()
