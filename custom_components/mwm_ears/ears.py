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
        effect_label,
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
        effect_label,
        frame_is_valid,
    )

BOTH = "both"
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

# Transmissions newer than this count as our own echo when overheard.
OURS_WINDOW_S = 15.0

# After we transmit a state-changing command, treat beacons heard within
# this window as STALE (they were already in flight when the ears processed
# the command) rather than as proof the ears are really on.  Without this,
# an explicit "all off" races the ear's last pre-off beacon: the off sticks
# on the hardware but the very next overheard beacon force-sets desired_on
# back to True and the entity slides back on.  About one beacon
# interval (~7-15 s) rides out the race; a genuinely re-woken ear keeps
# beaconing and correctly shows on again after the window.
BEACON_STALE_AFTER_COMMAND_S = 15.0

# Enforcement mode: when the per-room "Enforce Ears" switch is ON we take
# sole control of the ears -- re-asserting the light-entity state every
# ENFORCE_INTERVAL_S and NOT adopting foreign commands (a wand or other
# transmitter is overridden by the next re-assert).  If no beacon is heard
# for ENFORCE_ASSUME_OFF_S while enforcing, we assume the ears powered off
# and reflect that (light entities go off); a later beacon proves they are
# alive again and enforcement resumes the held colour.  The passive mode
# (switch OFF) keeps the existing reflect-what-we-hear behaviour and the
# long BEACON_TIMEOUT_S idle rule.
ENFORCE_INTERVAL_S = 10
ENFORCE_ASSUME_OFF_S = 45


_LOGGER = logging.getLogger(__name__)

RIGHT_ONLY_BASE = 0x68


class EarPairState:
    """Desired state of one transmitter's ear pair.

    User-initiated sends use repeat_count=BURST_REPEATS extra spaced
    transmissions.  When a foreign MWM command is overheard (wand,
    another transmitter), repetition suspends until the next explicit
    user action.  State is kept honest by detecting received IR frames
    rather than re-blasting commands periodically.
    """

    def __init__(
        self, transmit, *, repeat_gap_s: float = REPEAT_GAP_S, clock=time.monotonic
    ) -> None:
        # async (frame: bytes, repeat_count: int)
        self._transmit = transmit
        self.repeat_gap_s = repeat_gap_s
        self._clock = clock
        self.receiver_entity: str | None = None  # set by __init__ setup
        self.codes: dict[str, int] = {LEFT: EAR_OFF_CODE, RIGHT: EAR_OFF_CODE}
        self.room_name: str = ""  # set by __init__ setup
        self._last_active_at: float = 0.0
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
        self.listeners: list = []
        self._recent_sent: list[tuple[str, float]] = []
        # Last time we transmitted a state-changing command.  Beacons heard
        # shortly afterwards may be stale (see BEACON_STALE_AFTER_COMMAND_S).
        self._last_command_at: float = 0.0
        # Enforcement mode flag plus the last time this pair heard a beacon.
        # While enforcing we re-assert state every ENFORCE_INTERVAL_S and
        # assume the ears powered off after ENFORCE_ASSUME_OFF_S of silence.
        self.enforce = False
        self._last_beacon_at: float = 0.0

    # -- display ---------------------------------------------------------

    def _notify(self) -> None:
        self._last_active_at = self._clock()
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

    def side_hs_color(self, side: str) -> tuple[float, float] | None:
        """Return (hue, saturation) for the ear's current colour, or None.

        Returns None when an effect program is running (the effect
        controls colour, not stored codes) or when the ear is off.
        For BOTH, returns the colour only when left and right agree;
        returns None when they diverge.
        """
        if self.running_effect is not None:
            return None
        from homeassistant.util.color import color_RGB_to_hs

        def _hs_for(s: str) -> tuple[float, float] | None:
            pp = self.palette_code.get(s)
            if pp is not None:
                entry = PALETTE.get(pp)
                if entry and entry[1]:
                    return color_RGB_to_hs(*entry[1])
                return None
            code = self.codes.get(s, EAR_OFF_CODE)
            if code == EAR_OFF_CODE:
                return None
            entry = SIMPLE_COLORS.get(code)
            if entry and entry[1]:
                return color_RGB_to_hs(*entry[1])
            return None

        if side == BOTH:
            left = _hs_for(LEFT)
            right = _hs_for(RIGHT)
            return left if left == right else None
        return _hs_for(side)

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

    # -- enforcement mode ------------------------------------------------

    def set_enforce(self, on: bool) -> None:
        """Turn enforcement of the light-entity state on or off.

        ON: we become the sole authority -- foreign commands are overridden
        and the held state is re-asserted periodically (see
        async_enforce_tick).  OFF: revert to passive reflect-what-we-hear.
        The flag is purely user-controlled; it is not cleared by assume-off.
        """
        if self.enforce != on:
            self.enforce = on
            self.resume()  # enforcement pauses any suspension
            self._notify()

    def assume_off_if_silent(self) -> bool:
        """While enforcing, turn the ears off after a beacon gap.

        Used the per-pair last-beacon timestamp so rooms sharing one hub
        don't keep each other alive.  Returns True if state changed (the
        caller should notify HA).  No-op in passive mode.
        """
        if not self.enforce:
            return False
        if not any(self.desired_on.values()):
            return False
        if self._last_beacon_at == 0:
            return False  # never heard a beacon yet; don't clamp
        if self._clock() - self._last_beacon_at < ENFORCE_ASSUME_OFF_S:
            return False
        self.running_effect = None
        self.desired_on = {LEFT: False, RIGHT: False}
        self._notify()
        return True

    async def enforce_tick(self) -> None:
        """One enforcement heartbeat: re-assert the held state if on.

        Re-asserts with a single transmission (one pass, no extra repeats):
        the command recurs every ENFORCE_INTERVAL_S so ears that drift back
        to demo/standalone mode are pulled onto our colour again.  A dead
        pair (assumed-off) has desired_on False and sends nothing here.
        """
        if not self.enforce:
            return
        if any(self.desired_on.values()):
            await self._send_state(0)

    # -- commands ---------------------------------------------------------

    def _state_frames(self) -> list[bytes]:
        """Verified-form frames expressing the current pair state.

        Equal pairs use the canonical both-ears form (`90 6X`).  When
        left and right differ, a *fused* 2-byte phrase sets each ear in
        a single IR frame (samples/mwm-gwts-colors.tsv row
        ``left-blue-right-green-fused``; docs/mwm-gwts-protocol.md
        section "Left vs right ears"):

            ``91 <left> <right-only(right)>``

        The first byte (`60`-`67`) brings both ears to the left colour;
        the second byte (`68`-`6F`) overrides only the right ear.
        Multi-byte phrases run opcodes sequentially against both ears, so
        a both-ear opcode followed by a right-only opcode achieves
        per-side control in one burst.  Equal pairs need only the single
        both-ears form (including canonical ``90 60`` off).
        """
        left, right = self.codes[LEFT], self.codes[RIGHT]
        if left == right:
            return [build_frame([left])]
        return [build_frame([left, RIGHT_ONLY_BASE + (right - EAR_OFF_CODE)])]

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
                    self._last_command_at = self._clock()
                    self._notify()
                except Exception:  # noqa: BLE001 - keep the group going
                    _LOGGER.exception("transmit failed (pass %d)", attempt)

    async def _send_state(self, repeat_count: int) -> None:
        """Send the composed current pair state."""
        await self._send_group(self._state_frames(), repeat_count)

    async def _send(self, frame: bytes, repeat_count: int) -> None:
        await self._transmit(frame, repeat_count)
        self._mark_sent(frame)
        self._last_command_at = self._clock()
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

    async def apply_simple_both(self, code: int) -> None:
        """Canonical both-ears form (`90 6X`) -- the protocol's home turf."""
        self.palette_code = {LEFT: None, RIGHT: None}
        self.running_effect = None
        self.codes = {LEFT: code, RIGHT: code}
        on = code != EAR_OFF_CODE
        self.desired_on = {LEFT: on, RIGHT: on}
        if on:
            # Both sides genuinely received this shade.
            self.last_simple = {LEFT: code, RIGHT: code}
        self.resume()
        await self._send_state(BURST_REPEATS)

    async def turn_on_both(self) -> int:
        """Bare both-ears turn-on: restore the pair's remembered colour."""
        code = (
            self.last_simple[LEFT] or self.last_simple[RIGHT]
            or DEFAULT_COLOR_CODE
        )
        await self.apply_simple_both(code)
        return code

    def adopt_decoded(self, frame: bytes) -> bool:
        """Mirror a FOREIGN decoded command into displayed state (no TX).

        Keeps the lights honest when a wand or another transmitter takes
        over the room; suspension keeps our refresh quiet while they rule.
        Returns True when displayed state changed.
        """
        content = bytes(frame)[1:-1]
        desc = describe_frame(frame)
        changed = True

        if desc.get("kind") == "wand-command":
            # Wand/paintbrush phrases carry structured fields: either an
            # explicit palette shade or a named program whose visuals we
            # cannot reproduce frame-for-frame -- record what was chosen,
            # never invent ear colours the phrase did not state.
            pal = desc.get("palette")
            if pal:
                pp = pal["index"]
                if pal["scope"] == "right":
                    self.codes[RIGHT] = EAR_OFF_CODE
                    self.palette_code[RIGHT] = pp
                else:
                    self.codes = {LEFT: EAR_OFF_CODE, RIGHT: EAR_OFF_CODE}
                    self.palette_code = {LEFT: pp, RIGHT: pp}
                self.running_effect = None
            else:
                self.running_effect = desc["summary"]
                self.palette_code = {LEFT: None, RIGHT: None}
                self.desired_on = {LEFT: True, RIGHT: True}
            self._notify()
            return changed
        if len(content) == 1 and 0x60 <= content[0] <= 0x67:
            code = content[0]
            self.codes = {LEFT: code, RIGHT: code}
            self.palette_code = {LEFT: None, RIGHT: None}
            self.running_effect = None
            on = code != EAR_OFF_CODE
            self.desired_on = {LEFT: on, RIGHT: on}
        elif len(content) == 1 and 0x68 <= content[0] <= 0x6F:
            self.codes[RIGHT] = (
                EAR_OFF_CODE if content[0] == 0x68
                else EAR_OFF_CODE + content[0] - RIGHT_ONLY_BASE
            )
            self.palette_code[RIGHT] = None
        elif (
            len(content) == 2 and content[0] == 0x0E
            and content[1] & 0x7F <= 0x1D
        ):
            pp = content[1] & 0x7F
            if content[1] & 0x80:
                slots = {RIGHT: pp}
            else:
                self.codes = {LEFT: EAR_OFF_CODE, RIGHT: EAR_OFF_CODE}
                slots = {LEFT: pp, RIGHT: pp}
            self.palette_code.update(slots)
            self.running_effect = None
            self.desired_on = {LEFT: True, RIGHT: True}
        elif len(content) == 2 and content[0] == 0x48:
            self.running_effect = effect_label(content[1])
            self.palette_code = {LEFT: None, RIGHT: None}
            self.desired_on = {LEFT: True, RIGHT: True}
        elif (
            len(content) == 3 and content[0] == RESET_OPCODE
            and content[1] == 0x48
        ):
            self.running_effect = effect_label(content[2])
            self.palette_code = {LEFT: None, RIGHT: None}
            self.desired_on = {LEFT: True, RIGHT: True}
        elif desc.get("effect") is not None:
            # Any other shape whose payload names an effect program
            # (long wand scripts etc.): record the program even when the
            # colour choreography itself stays opaque to us.
            self.running_effect = effect_label(desc["effect"])
            self.palette_code = {LEFT: None, RIGHT: None}
            self.desired_on = {LEFT: True, RIGHT: True}
        elif len(content) == 1 and content[0] == RESET_OPCODE:
            # Doc section 4: a bare `24` blacks the ears (escape price).
            self.codes = {LEFT: EAR_OFF_CODE, RIGHT: EAR_OFF_CODE}
            self.palette_code = {LEFT: None, RIGHT: None}
            self.running_effect = None
            self.desired_on = {LEFT: False, RIGHT: False}
        else:
            changed = False
        if changed:
            self._notify()
        return changed

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
        the both+right-only composition.  Already-dark ears are a no-op
        with no transmission: HA fires turn_off liberally (automations,
        area off, stale restored state) and re-bursting here once
        produced surprise all-off commands.
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



def extract_timing_candidates(payload) -> list[list[int]]:
    """Pull every plausible raw-timing sequence out of a signal payload.

    The infrared framework's signal objects have grown fields over time
    (timings / raw_timings / raw / ...), and MQTT-template bridges
    sometimes leak non-numeric junk (Tasmota's compact letter codes)
    into otherwise numeric lists. Returns one cleaned list per source
    attribute found, in priority order.
    """
    sources: list = []
    if isinstance(payload, dict):
        getter = payload.get
        keys = payload.keys()
    else:
        getter = lambda name, _default=None: getattr(  # noqa: E731
            payload, name, _default
        )
        keys = dir(payload)
    for name in ("timings", "raw_timings", "raw", "levels"):
        value = getter(name)
        if isinstance(value, (list, tuple)):
            sources.append(list(value))
    cleaned: list[list[int]] = []
    for src in sources:
        nums: list[int] = []
        for item in src:
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)):
                nums.append(int(item))
            elif isinstance(item, str):
                # Tolerate "+415", "-380", "415"; silently drop letter
                # tokens such as Tasmota's compact repeat codes ("dC").
                try:
                    nums.append(int(float(item.strip())))
                except ValueError:
                    continue
        if nums:
            # Same capture often surfaces under several attribute names;
            # keep the first occurrence only so frames aren't ingested
            # twice from one signal.
            if nums not in cleaned:
                cleaned.append(nums)
    return cleaned


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
        self.last_beacon_at: float = 0.0

    def _notify(self) -> None:
        for callback in self.listeners:
            callback()

    def ingest(
        self, frames: list[bytes], *, receiver: str | None = None,
    ) -> None:
        """Feed decoded frames from ONE receiver into the shared view.

        *receiver* is the entity_id of the receiver that heard the signal.
        When provided, foreign-command adoption and echo detection are
        scoped to pairs bound to that receiver, preventing cross-room
        pollution when multiple config entries share one hub.

        A-B-A' bundles are ONE logical command: the tracker sees all
        three passes, but only the phrase (frames[0]) drives adoption --
        the companion block is parameters, not a command, and A' merely
        repeats A with a rolling counter tail.
        """
        target_pairs = [
            p for p in self.pairs
            if receiver is None or p.receiver_entity == receiver
        ]
        valid: list[bytes] = []
        for frame in frames:
            ok, _ = frame_is_valid(frame)
            if ok:
                valid.append(bytes(frame))
            else:
                self.invalid += 1
        if not valid:
            return
        bundle = describe_bundle(valid)
        command_frames = [valid[0]] if bundle else valid
        changed = False
        for frame in valid:
            desc = describe_frame(frame)
            self.seen += 1
            changed = True
            self.tracker.feed_frame(frame)
            self.last_summary = f"[{desc['kind']}] {desc['summary']}"
            if desc["kind"] == "beacon":
                self.last_beacon_at = self._clock()
                # Propagate the beacon's active-ear state to pairs bound
                # to this receiver.  target_pairs is already scoped by the
                # receiver entity, so only the correct room's pairs are
                # touched — cross-room pollution is not possible.
                demo_effect = desc.get("demo_effect")
                for pair in target_pairs:
                    # The effect/alive observations always update; they are
                    # what the sensor/snapshot report.  But we must NOT
                    # re-assert desired_on=True from a beacon heard right
                    # after our own command: that beacon was likely already
                    # in flight when the ears processed e.g. an "all off",
                    # so it would slide the entity straight back on.  Only
                    # beacons arriving AFTER the stale window (i.e. the
                    # ears genuinely kept/woke beaconing) flip desired_on.
                    now = self._clock()
                    # Every beacon (ours or foreign) proves the ears are
                    # alive; enforcement uses this per-pair liveness so a
                    # room sharing the hub is not kept alive by another.
                    pair._last_beacon_at = now
                    stale = (
                        now - pair._last_command_at
                        <= BEACON_STALE_AFTER_COMMAND_S
                    )
                    if not stale:
                        pair.desired_on = {LEFT: True, RIGHT: True}
                    pair.running_effect = (
                        effect_label(demo_effect) if demo_effect is not None
                        else None
                    )
                    pair.palette_code = {LEFT: None, RIGHT: None}
        for frame in command_frames:
            desc = describe_frame(frame)
            if desc["kind"] == "beacon":
                continue
            frame_hex = frame.hex().upper()
            if any(pair.matches_recent(frame_hex) for pair in target_pairs):
                continue  # our own echo bouncing back
            # Foreign command (wand / other transmitter): in passive mode,
            # mirror what we understood into the displayed state, then
            # suspend repeats until the next explicit user action.  While a
            # room is ENFORCING we take sole control: we record the foreign
            # traffic for the diagnostic sensor but do not adopt it or pause
            # -- the next enforce_tick re-asserts our held state.
            self.last_foreign_summary = f"[{desc['kind']}] {desc['summary']}"
            for pair in target_pairs:
                if pair.enforce:
                    continue
                pair.adopt_decoded(frame)
                pair.suspend(f"foreign command: {desc['summary']}")
        if changed:
            self._notify()

    def snapshot(self) -> str:
        snap = self.tracker.snapshot()
        return snap if snap else "unknown"

    def check_beacon_timeout(self, timeout_s: float) -> bool:
        """Clear ear state when no beacon has arrived for *timeout_s*.

        Returns True if state was changed (listeners should be notified).
        """
        if self.last_beacon_at == 0:
            return False  # never heard a beacon yet; don't clamp
        elapsed = self._clock() - self.last_beacon_at
        if elapsed < timeout_s:
            return False
        changed = False
        for pair in self.pairs:
            if pair.enforce:
                continue  # enforcement owns its own assume-off rule
            if any(pair.desired_on.values()) or pair.running_effect:
                pair.running_effect = None
                pair.desired_on = {LEFT: False, RIGHT: False}
                pair._notify()
                changed = True
        return changed


class ReceiverData:
    """Per-receiver-entry counters for its diagnostic sensors."""

    def __init__(self) -> None:
        self.listeners: list = []
        self.message_count = 0
        self.invalid_count = 0
        self.signals_seen = 0
        self.last_signal_debug: dict | None = None
        self.rebinds = 0
        self.last_is_bundle = False
        self.last_phrase_hex: str | None = None
        self.last_companion_hex: str | None = None
        self.last_frames_hex = ""
        self.last_summary = ""
        self.last_frames_desc: list[dict] = []
        self.last_bundle_desc: dict | None = None

    def note_signal(self, payload, candidates: list[list[int]]) -> None:
        """Census one received signal regardless of decodability.

        Notifies sensor listeners so diagnostic counters (signals_seen,
        last_signal_debug) refresh on every received signal, even when
        decode_timings produces no frames.
        """
        self.signals_seen += 1
        self.last_signal_debug = {
            "payload_type": type(payload).__name__,
            "attributes": (
                sorted(k for k in vars(payload))  # instance attrs only
                if hasattr(payload, "__dict__") else
                sorted(payload.keys()) if isinstance(payload, dict) else None
            ),
            "candidate_lengths": [len(c) for c in candidates],
            "head": candidates[0][:6] if candidates else None,
        }
        for callback in self.listeners:
            callback()

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
            self.last_frames_desc = [
                describe_frame(f) for f in valid_frames
            ]
            bundle = describe_bundle(valid_frames)
            self.last_is_bundle = bundle is not None
            self.last_bundle_desc = bundle
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
