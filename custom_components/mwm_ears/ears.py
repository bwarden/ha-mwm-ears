"""Pure state logic for ear-pair control and overheard MWM traffic.

Deliberately free of Home Assistant imports so the refresh/suspension
rules are unit-testable standalone: transmission is an injected async
callable and clocks are injectable. The HA glue in __init__.py wires
these objects to infrared emitter/receiver entities.
"""

from __future__ import annotations

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
DEFAULT_COLOR_CODE = 0x67  # white

# Rig-verified palette template (samples/mwm-gwts-colors.tsv rows
# "palette-XX-both"): sets palette color pp on both ears simultaneously.
_PALETTE_TEMPLATE = [0x19, 0x07, 0x0F, 0x16]
_PALETTE_TEMPLATE_TAIL = [0x18, 0x04]

# Extra transmissions per user-initiated send (initial multi-burst).
BURST_REPEATS = 2
# Periodic re-issue cadence so late joiners sync; matches ear-hat beacon
# pacing (~7-12 s).
DEFAULT_REFRESH_S = 8.0
# Transmissions newer than this count as our own echo when overheard.
OURS_WINDOW_S = 15.0


class EarPairState:
    """Desired state of one transmitter's ear pair plus repeat policy.

    Refresh semantics:

    - user-initiated sends use a multi-burst (repeat_count=BURST_REPEATS);
    - while BOTH ears are on, a periodic tick re-issues the current fused
      colour phrase once so newly-powered ears join in;
    - any off state is sent only as its initial burst and never repeated,
      so independently-controlled ears are left alone;
    - effect invocations are never re-issued (restarting a running program
      every few seconds would glitch it);
    - when a foreign MWM command is overheard (wand, another transmitter),
      repetition suspends until the next explicit user action.
    """

    def __init__(self, transmit, *, clock=time.monotonic) -> None:
        self._transmit = transmit  # async (frame: bytes, repeat_count: int)
        self._clock = clock
        self.codes: dict[str, int] = {LEFT: EAR_OFF_CODE, RIGHT: EAR_OFF_CODE}
        self.desired_on: dict[str, bool] = {LEFT: False, RIGHT: False}
        # Last explicitly chosen simple color per side (None = never set);
        # bare turn-ons restore it. Palette shades are not remembered here
        # because they apply to both ears and cannot restore per-side.
        self.last_simple: dict[str, int | None] = {LEFT: None, RIGHT: None}
        self.palette_index: int | None = None
        self.running_effect: str | None = None
        self.suspended_by: str | None = None
        self.refresh_interval: float = DEFAULT_REFRESH_S
        self.listeners: list = []
        self._last_sent: tuple[str, float] | None = None

    # -- display ---------------------------------------------------------

    def _notify(self) -> None:
        for callback in self.listeners:
            callback()

    def side_color_name(self, side: str) -> str:
        if self.palette_index is not None:
            entry = PALETTE.get(self.palette_index)
            return entry[0] if entry else "unknown"
        code = self.codes[side]
        if code == EAR_OFF_CODE:
            return EAR_STATE_OFF
        return SIMPLE_COLORS[code][0] if code in SIMPLE_COLORS else "unknown"

    # -- ours-vs-foreign discrimination ----------------------------------

    def _mark_sent(self, frame: bytes) -> None:
        self._last_sent = (bytes(frame).hex().upper(), self._clock())

    def matches_recent(self, frame_hex: str) -> bool:
        if not self._last_sent:
            return False
        hex_seen, seen_at = self._last_sent
        return (
            hex_seen == frame_hex.upper()
            and self._clock() - seen_at <= OURS_WINDOW_S
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

        Equal pairs use the canonical one-byte simple forms (`90 6X`,
        including the both-off keep-alive `90 60`); differing pairs use the
        rig-verified fused phrase (`91 left right`, cf.
        samples/mwm-gwts-colors.tsv). Unverified combinations never go out.
        """
        left, right = self.codes[LEFT], self.codes[RIGHT]
        if left == right:
            return [build_frame([left])]
        return [build_frame([left, right])]

    async def _send_state(self, repeat_count: int) -> None:
        for frame in self._state_frames():
            await self._send(frame, repeat_count)

    async def _send(self, frame: bytes, repeat_count: int) -> None:
        await self._transmit(frame, repeat_count)
        self._mark_sent(frame)
        self._notify()

    async def apply_simple(self, side: str, code: int) -> None:
        """Set one ear's simple color (0x60 off .. 0x67 white), fused."""
        self.palette_index = None
        self.running_effect = None
        self.codes[side] = code
        self.desired_on[side] = code != EAR_OFF_CODE
        if code != EAR_OFF_CODE:
            self.last_simple[side] = code
        self.resume()
        await self._send_state(BURST_REPEATS)

    async def apply_palette(self, index: int) -> None:
        """Apply a palette shade to both ears (no verified per-ear form)."""
        self.palette_index = index
        self.running_effect = None
        self.desired_on = {LEFT: True, RIGHT: True}
        self.resume()
        await self._send(
            build_frame(_PALETTE_TEMPLATE + [index] + _PALETTE_TEMPLATE_TAIL),
            BURST_REPEATS,
        )

    async def apply_effect(self, index: int, label: str) -> None:
        # 24 lets an invocation take effect while a built-in program runs;
        # park captures show bare 48 XX phrases working as well.
        self.running_effect = label
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
        """Turn one ear off via its half of the fused phrase.

        Sent as a single initial multi-burst only: refresh stays quiet
        unless both ears are on, so off states are never re-issued.
        Already-dark ears are a no-op with no transmission: HA fires
        turn_off liberally (automations, area off, stale restored state)
        and re-bursting the fused phrase here once produced surprise
        all-off commands.
        """
        if self.codes[side] == EAR_OFF_CODE:
            self.desired_on[side] = False
            return
        self.codes[side] = EAR_OFF_CODE
        self.desired_on[side] = False
        if not any(self.desired_on.values()):
            self.running_effect = None
        self.resume()
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
            # report that as ONE logical command instead of three lines.
            bundle = describe_bundle(valid_frames)
            self.last_is_bundle = bundle is not None
            self.last_summary = (
                bundle["summary"] if bundle else "; ".join(summaries)
            )
            for callback in self.listeners:
                callback()
