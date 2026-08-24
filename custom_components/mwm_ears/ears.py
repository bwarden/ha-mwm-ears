"""Pure state logic for ear-pair control and overheard MWM traffic.

Deliberately free of Home Assistant imports so the refresh/suspension
rules are unit-testable standalone: transmission is an injected async
callable and clocks are injectable. The HA glue in __init__.py wires
these objects to infrared emitter/receiver entities.
"""

from __future__ import annotations

import asyncio
import logging
import time

if __package__:  # normal HA component context
    from ._mwm import (
        EAR_STATE_OFF,
        PALETTE,
        SIMPLE_COLORS,
        EarStateTracker,
        build_frame,
        describe_bundle,
        describe_frame,
        frame_is_valid,
    )
else:  # standalone test harness: _bootstrap registers us as "mwm"
    from mwm import (
        EAR_STATE_OFF,
        PALETTE,
        SIMPLE_COLORS,
        EarStateTracker,
        build_frame,
        describe_bundle,
        describe_frame,
        frame_is_valid,
    )

LEFT = "left"
RIGHT = "right"

EAR_OFF_CODE = 0x60
RESET_OPCODE = 0x24   # flow-control override (doc section 4)
DEFAULT_COLOR_CODE = 0x67  # white

# Rig-verified palette template (samples/mwm-gwts-colors.tsv rows
# "palette-XX-both"): sets palette color pp on both ears simultaneously.
_PALETTE_TEMPLATE = [0x19, 0x07, 0x0F, 0x16]
_PALETTE_TEMPLATE_TAIL = [0x18, 0x04]

# Total transmissions per logical command pass, REPEAT_GAP_S apart:
# ir-mwm-send's proven default (--repeat 2). Two passes -- the first
# warms cold receivers, the second lands.
BURST_REPEATS = 1
# Seconds between grouped re-transmissions of one logical command
# (mirrors perl ir-mwm-send's proven 2x @ ~1.8 s recipe).
REPEAT_GAP_S = 1.8
# Periodic re-issue cadence so late joiners sync; matches ear-hat beacon
# pacing (~7-12 s).
DEFAULT_REFRESH_S = 8.0
# Transmissions newer than this count as our own echo when overheard.
OURS_WINDOW_S = 15.0


_LOGGER = logging.getLogger(__name__)

RIGHT_ONLY_BASE = 0x68


class EarPairState:
    """Desired state of one transmitter's ear pair plus repeat policy.

    Refresh semantics:

    - user-initiated sends use repeat_count=BURST_REPEATS extra spaced
  transmissions;
    - while BOTH ears are on, a periodic tick re-issues the current
      composed colour pair once so newly-powered ears join in;
    - any off state is sent only as its initial burst and never repeated,
      so independently-controlled ears are left alone;
    - effect invocations are never re-issued (restarting a running program
      every few seconds would glitch it);
    - when a foreign MWM command is overheard (wand, another transmitter),
      repetition suspends until the next explicit user action.
    """

    def __init__(
        self, transmit, *, repeat_gap_s: float = REPEAT_GAP_S, clock=time.monotonic
    ) -> None:
        # async (frame: bytes, repeat_count: int)
        self._transmit = transmit
        self.repeat_gap_s = repeat_gap_s
        self._clock = clock
        self.codes: dict[str, int] = {LEFT: EAR_OFF_CODE, RIGHT: EAR_OFF_CODE}
        # Active PALETTE shade per side (None = simple/unknown). Palette
        # shades are tracked separately from simple codes because a side
        # can hold either; TSV has both-ears AND right-only palette forms.
        self.palette_code: dict[str, int | None] = {LEFT: None, RIGHT: None}
        self.desired_on: dict[str, bool] = {LEFT: False, RIGHT: False}
        # Last explicitly chosen simple color per side (None = never set);
        # bare turn-ons restore it. Palette shades are not remembered here
        # because they apply to both ears and cannot restore per-side.
        self.last_simple: dict[str, int | None] = {LEFT: None, RIGHT: None}
        self.running_effect: str | None = None
        self.suspended_by: str | None = None
        self.refresh_interval: float = DEFAULT_REFRESH_S
        self.listeners: list = []
        self._recent_sent: list[tuple[str, float]] = []

    # -- display ---------------------------------------------------------

    def _notify(self) -> None:
        for callback in self.listeners:
            callback()

    def side_color_name(self, side: str) -> str:
        if self.palette_code.get(side) is not None:
            entry = PALETTE.get(self.palette_code[side])
            return entry[0] if entry else "unknown"
        code = self.codes[side]
        if code == EAR_OFF_CODE:
            return EAR_STATE_OFF
        return SIMPLE_COLORS[code][0] if code in SIMPLE_COLORS else "unknown"

    # -- ours-vs-foreign discrimination ----------------------------------

    def _mark_sent(self, frame: bytes) -> None:
        # Composed commands are MULTI-frame groups; remember each frame
        # so echoes of any member are recognised as ours.
        self._recent_sent.append((bytes(frame).hex().upper(), self._clock()))
        del self._recent_sent[:-16]

    def matches_recent(self, frame_hex: str) -> bool:
        now = self._clock()
        return any(
            hex_seen == frame_hex.upper() and now - seen_at <= OURS_WINDOW_S
            for hex_seen, seen_at in reversed(self._recent_sent)
        )

    def suspend(self, reason: str) -> None:
        if self.suspended_by != reason:
            self.suspended_by = reason
            self._notify()

    def resume(self) -> None:
        if self.suspended_by is not None:
            self.suspended_by = None
            self._notify()

    # -- commands ---------------------------------------------------------

    def _state_frames(self) -> list[bytes]:
        """Verified-form frames expressing the current pair state.

        The protocol's simple-colour primitives target BOTH ears (`90 6X`)
        or the RIGHT ear alone (`90 68+X`; samples/mwm-gwts-colors.tsv).
        There is no per-side fused phrase: multi-byte phrases run their
        opcodes in order against both ears (rig session 2026-08-23:
        `91 62 61` -> both blue, `91 62 60` -> both dark), so an embedded
        off byte always wins eventually. Left-ear changes are therefore a
        COORDINATION of the two primitives:

            1. `90 <left>`   -- bring both ears to the left colour,
            2. `90 <right-only(right)>` -- restore the right ear alone.

        Equal pairs need only step 1 (including canonical `90 60` off).
        """
        left, right = self.codes[LEFT], self.codes[RIGHT]
        if left == right:
            return [build_frame([left])]
        return [
            build_frame([left]),
            build_frame([RIGHT_ONLY_BASE + (right - EAR_OFF_CODE)]),
        ]

    async def _send_group(
        self, frames: list[bytes], repeat_count: int
    ) -> None:
        """Transmit frames as ONE logical command, repeated as a GROUP.

        Every pass runs the full sequence back-to-back (the frames'
        built-in footers provide inter-message spacing); passes are
        spaced by the rig-proven gap so cold receivers get a warm-up
        pass. One failed pass logs and continues: partial IR still
        lands and the remaining passes heal it.
        """
        for attempt in range(repeat_count + 1):
            if attempt:
                await asyncio.sleep(self.repeat_gap_s)
            for frame in frames:
                try:
                    await self._transmit(frame, 0)
                    self._mark_sent(frame)
                    self._notify()
                except Exception:  # noqa: BLE001 - keep the group going
                    _LOGGER.exception("transmit failed (pass %d)", attempt)

    async def _send_state(self, repeat_count: int) -> None:
        """Send the composed current pair state."""
        await self._send_group(self._state_frames(), repeat_count)

    async def _send(self, frame: bytes, repeat_count: int) -> None:
        await self._transmit(frame, repeat_count)
        self._mark_sent(frame)
        self._notify()

    async def apply_simple(self, side: str, code: int) -> None:
        """Set one ear's simple color (0x60 off .. 0x67 white).

        Canonical TSV frames only (rig session 2026-08-23 landed them
        instantly WITHOUT any leading 24 override; the override's flow
        control blacks the ears for seconds -- undesirable here). When
        ONLY the right slot changes we send the single right-only form:
        no intermediate flash, the left ear never hears a thing.
        """
        old_left = self.codes[LEFT]
        self.palette_code[side] = None
        self.running_effect = None
        self.codes[side] = code
        self.desired_on[side] = code != EAR_OFF_CODE
        if code != EAR_OFF_CODE:
            self.last_simple[side] = code
        self.resume()
        if (
            side == RIGHT
            and old_left == self.codes[LEFT]
            and old_left != code  # equal pairs use the canonical form
            and EAR_OFF_CODE <= code <= 0x67
        ):
            frames = [build_frame([RIGHT_ONLY_BASE + code - EAR_OFF_CODE])]
            await self._send_group(frames, BURST_REPEATS)
            return
        await self._send_state(BURST_REPEATS)

    async def apply_palette(self, index: int, side: str | None = None) -> None:
        """Apply a palette shade using the TSV short forms only.

        Both-ears: `91 0E pp`; RIGHT-only: `91 0E pp|80` (samples/
        mwm-gwts-colors.tsv). A LEFT pick composes [both -> index]
        [right-only restore] when the right ear currently holds a
        palette shade; otherwise it degrades to the both-ears form
        because a simple colour cannot be re-expressed as a palette
        shade (protocol limitation).
        """
        target = side or "both"

        def frame(pp: int) -> bytes:
            return build_frame([0x0E, pp])

        if target == RIGHT:
            self.palette_code[RIGHT] = index
            self.running_effect = None
            self.desired_on[RIGHT] = True
            self.resume()
            await self._send_group([frame(index | 0x80)], BURST_REPEATS)
            return

        restore_right = (
            target == LEFT and self.palette_code[RIGHT] is not None
        )
        self.palette_code[LEFT] = index
        if not restore_right:
            self.palette_code[RIGHT] = index
        self.running_effect = None
        self.desired_on = {LEFT: True, RIGHT: True}
        self.resume()
        frames = [frame(index)]
        if restore_right:
            frames.append(frame(self.palette_code[RIGHT] | 0x80))
        await self._send_group(frames, BURST_REPEATS)

    async def apply_effect(self, index: int, label: str) -> None:
        # 24 lets an invocation take effect while a built-in program runs;
        # park captures show bare 48 XX phrases working as well.
        self.running_effect = label
        self.palette_code = {LEFT: None, RIGHT: None}
        self.desired_on = {LEFT: True, RIGHT: True}
        self.resume()
        await self._send(build_frame([0x24, 0x48, index]), BURST_REPEATS)

    async def turn_on_side(self, side: str) -> int:
        """Light one ear with its remembered simple color.

        Bare HA turn-ons carry no color; re-issue the side's last explicit
        pick so the light state matches reality again, defaulting to white
        when nothing was ever chosen. Returns the code sent.
        """
        code = self.last_simple.get(side) or DEFAULT_COLOR_CODE
        await self.apply_simple(side, code)
        return code

    async def turn_off_side(self, side: str) -> None:
        """Turn one ear off.

        Routes through _send_state, so the other ear keeps its colour via
        the both+right-only composition. Sent as a grouped burst only:
        refresh stays quiet unless both ears are on, so off states are
        never re-issued. Already-dark ears are a no-op with no
        transmission: HA fires turn_off liberally (automations, area off,
        stale restored state) and re-bursting here once produced surprise
        all-off commands.
        """
        if self.codes[side] == EAR_OFF_CODE:
            self.desired_on[side] = False
            return
        self.codes[side] = EAR_OFF_CODE
        self.palette_code[side] = None
        self.desired_on[side] = False
        if not any(self.desired_on.values()):
            self.running_effect = None
        self.resume()
        if side == RIGHT:
            # Right-only OFF is a verified single form (`90 68`): the left
            # ear stays untouched -- no compose flash.
            await self._send_group(
                [build_frame([RIGHT_ONLY_BASE])], BURST_REPEATS
            )
            return
        await self._send_state(BURST_REPEATS)

    # -- periodic refresh --------------------------------------------------

    @property
    def should_refresh(self) -> bool:
        # Gate on ACTUAL colours, not desired_on: apply_palette arms
        # desired_on while codes stay off -- refreshing then would spam
        # the all-off keep-alive over the palette shade every tick
        # (observed live: colour picks "did nothing" because this buried
        # them within 8 seconds).
        return (
            all(self.codes[side] != EAR_OFF_CODE for side in (LEFT, RIGHT))
            and self.suspended_by is None
        )

    async def refresh_tick(self) -> bool:
        """Re-issue the current colour pair for late joiners.

        Returns True when a frame went out. Only fully-on pairs refresh:
        mixed or all-off pairs must not have their off halves repeated.
        """
        if not self.should_refresh:
            return False
        for frame in self._state_frames():
            await self._transmit(frame, 0)
            self._mark_sent(frame)
        return True


class ObservedHub:
    """Room-level view of overheard MWM traffic across all receivers.

    Feeds a shared EarStateTracker with every decoded frame, distinguishes
    our own echoes from foreign commands using each pair's recent-send
    window, and suspends repetition when something else takes control.
    """

    def __init__(self, *, clock=time.monotonic) -> None:
        self.tracker = EarStateTracker()
        self.pairs: list[EarPairState] = []
        self.listeners: list = []
        self.seen = 0
        self.invalid = 0
        self.last_summary = ""
        self.last_foreign_summary = ""
        self._clock = clock

    def _notify(self) -> None:
        for callback in self.listeners:
            callback()

    def ingest(self, frames: list[bytes]) -> None:
        """Feed decoded frames from ANY receiver into the shared view."""
        changed = False
        for frame in frames:
            ok, _ = frame_is_valid(frame)
            if not ok:
                self.invalid += 1
                continue
            desc = describe_frame(frame)
            self.seen += 1
            changed = True
            self.tracker.feed_frame(frame)
            self.last_summary = f"[{desc['kind']}] {desc['summary']}"
            if desc["kind"] == "beacon":
                continue  # idle sync: effect display only, no takeover
            frame_hex = bytes(frame).hex().upper()
            if any(pair.matches_recent(frame_hex) for pair in self.pairs):
                continue  # our own echo bouncing back
            # Foreign command (wand / other transmitter): suspend repeats
            # and adopt what we can understand of it.
            self.last_foreign_summary = self.last_summary
            for pair in self.pairs:
                pair.suspend(f"foreign command: {desc['summary']}")
        if changed:
            self._notify()

    def snapshot(self) -> str:
        snap = self.tracker.snapshot()
        return snap if snap else "unknown"


class ReceiverData:
    """Per-receiver-entry counters for its diagnostic sensors."""

    def __init__(self) -> None:
        self.listeners: list = []
        self.message_count = 0
        self.invalid_count = 0
        self.last_is_bundle = False
        self.last_phrase_hex: str | None = None
        self.last_companion_hex: str | None = None
        self.last_frames_hex = ""
        self.last_summary = ""

    def ingest(self, frames: list[bytes]) -> None:
        valid_frames: list[bytes] = []
        summaries: list[str] = []
        for frame in frames:
            ok, _ = frame_is_valid(frame)
            if not ok:
                self.invalid_count += 1
                continue
            valid_frames.append(frame)
            summaries.append(describe_frame(frame)["summary"])
        self.message_count += len(frames)
        if valid_frames:
            self.last_frames_hex = "+".join(
                f.hex().upper() for f in valid_frames
            )
            # Wand/ear commands arrive as [phrase][companion][phrase];
            # report that as ONE logical command instead of three lines,
            # keeping the A (phrase) and B (companion) parts separately
            # visible -- A' is omitted when identical to A.
            bundle = describe_bundle(valid_frames)
            self.last_is_bundle = bundle is not None
            if bundle:
                self.last_phrase_hex = bundle["phrase_hex"]
                self.last_companion_hex = bundle["companion_hex"]
            else:
                self.last_phrase_hex = "+".join(
                    f.hex().upper() for f in valid_frames
                )
                self.last_companion_hex = None
            self.last_summary = (
                bundle["summary"] if bundle else "; ".join(summaries)
            )
            for callback in self.listeners:
                callback()
