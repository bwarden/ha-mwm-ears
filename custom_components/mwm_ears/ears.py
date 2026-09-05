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
        DEFAULT_COLOR_CODE,
        EAR_OFF_CODE,
        RESET_OPCODE,
        LEFT_ONLY_BASE,
        EAR_STATE_OFF,
        PALETTE,
        SIMPLE_COLORS,
        EFFECTS,
        EFFECT_COMPANION,
        EarStateTracker,
        build_frame,
        build_pulse,
        build_strobe,
        build_fade,
        rotation_phrase,
        describe_bundle,
        describe_frame,
        effect_label,
        frame_is_valid,
    )
else:  # standalone test harness: _bootstrap registers us as "mwm"
    from mwm import (
        DEFAULT_COLOR_CODE,
        EAR_OFF_CODE,
        RESET_OPCODE,
        LEFT_ONLY_BASE,
        EAR_STATE_OFF,
        PALETTE,
        SIMPLE_COLORS,
        EFFECTS,
        EFFECT_COMPANION,
        EarStateTracker,
        build_frame,
        build_pulse,
        build_strobe,
        build_fade,
        rotation_phrase,
        describe_bundle,
        describe_frame,
        effect_label,
        frame_is_valid,
    )

BOTH = "both"
LEFT = "left"
RIGHT = "right"

# Extra transmissions per logical command beyond the first, sent IMMEDIATELY
# back-to-back (the frames' footers space them; no pause between repeats so
# rapid color-wheel browsing isn't held up). Mirrors ir-mwm-send which
# repeats each frame; the extra pass covers a cold receiver / dropped IR.
BURST_REPEATS = 1
# Kept for API/prototype compatibility (constructor default); the group
# sender no longer pauses between passes.
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
# sole control of the ears -- re-asserting the light-entity state and NOT
# adopting foreign commands (overridden by the re-assert).  Our own
# transmitted echoes are ignored (matches_recent).  The passive mode
# (switch OFF) keeps the existing reflect-what-we-hear behavior, and the
# silence rule -- if no beacon is heard for BEACON_TIMEOUT_S while a light
# is on, assume the ears powered off -- applies only there; enforcement
# never assumes off.
ENFORCE_INTERVAL_S = 10
# Re-assert cadence when the enforced state is OFF.  Ears are silent ~2 min
# after a command, then fall back into demo/beaconing mode and show color;
# re-asserting OFF every ~90 s (well inside that window) keeps them dark by
# perpetually resetting the demo-entry timer.  An enforced ON state is
# re-asserted on the fast ENFORCE_INTERVAL_S tick instead (to hold color
# against drift).  Any foreign (non-echo) phrase triggers an IMMEDIATE
# re-assert that restarts the back-off (see enforce_reassert).
ENFORCE_OFF_INTERVAL_S = 90


_LOGGER = logging.getLogger(__name__)

# The `58 tt` cycle-timer companion an effect program needs in the SAME
# phrase to run at all is owned by the protocol library (mwm.EFFECTS /
# EFFECT_COMPANION) -- see docs/mwm-show-protocol.md section 4.  `48 04`
# Pulse requires `58 F0`; `48 03` Single flash deliberately has none (dig
# probe 2026-09-03: `48 03 58 F0` + color is fatal, plain `48 03` + color
# works).  The integration reads EFFECT_COMPANION rather than re-deriving it.


class EarPairState:
    """Desired state of one transmitter's ear pair.

    User-initiated sends use repeat_count=BURST_REPEATS extra back-to-back
    transmissions.  In passive mode (the default) state is kept honest by
    detecting received IR frames rather than re-blasting periodically: a
    foreign MWM command overheard (wand, another transmitter) suspends
    repetition until the next explicit user action.  In enforcement mode
    (set_enforce) we instead re-assert the held state every interval,
    whether it is on or off.
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
        # can hold either; TSV has both-ears AND left-only palette forms.
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
        # Last time this pair heard a beacon.  The passive assume-off rule
        # (assume_off_if_silent) turns the lights off after BEACON_TIMEOUT_S
        # of silence; keeping this per-pair stops one room keeping another
        # alive when they share a hub.
        self._last_beacon_at: float = 0.0
        # Last time enforcement actually transmitted a re-assert.  Drives
        # the OFF-target back-off (see enforce_tick); both the periodic tick
        # and the immediate enforce_reassert update it.
        self._last_reassert_at: float = 0.0
        # Enforcement mode flag: when True the pair is always re-asserted
        # (on or off) and foreign commands are overridden rather than
        # adopted.
        self.enforce = False
        # The exact IR frames of the last command that drove the display --
        # a user pick OR an adopted foreign frame.  Enforcement re-asserts
        # THESE (not a passive re-derivation) so it faithfully holds whatever
        # was last asked for -- simple color, palette shade, or effect
        # program -- and so that switching enforcement ON after a foreign
        # wand took over re-anchors to the adopted state instead of
        # re-blasting a stale pre-wand command.  `_state_frames()` (which
        # composes palette shades into fused frames too) is the fallback
        # when no command has ever been issued or adopted (fresh restart
        # while enforcing defaults to all-off).  Set at every user-command
        # site (via _send_state / callers) and at every adoption site
        # (adopt_decoded).
        self._hold_frames: list[bytes] | None = None

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
        """Return (hue, saturation) for the ear's current color, or None.

        Returns None when an effect program is running (the effect
        controls color, not stored codes) or when the ear is off.
        For BOTH, returns the color only when left and right agree;
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

    def side_picked(self, side: str) -> dict | None:
        """Exact identity of the color currently on ``side``, or None.

        Returns the catalog shape the frontend card highlights against:
        ``{"kind": "simple", "code": N}`` or ``{"kind": "palette",
        "index": N}``.  Near-identical shades that are DISTINCT protocol
        commands (simple 0x61 "blue" vs palette 0x04 "pure blue") must stay
        distinct, so this is by code/index -- never by RGB, which cannot
        tell a 0xFE from a 0xFF through the HS round-trip.  Mirrors
        ``side_hs_color``'s empty cases: off and running effects yield None,
        and BOTH yields the identity only when the left and right ears agree.
        """
        if self.running_effect is not None:
            return None

        def _picked_for(s: str) -> dict | None:
            pp = self.palette_code.get(s)
            if pp is not None and pp in PALETTE:
                return {"kind": "palette", "index": pp}
            code = self.codes.get(s, EAR_OFF_CODE)
            if code == EAR_OFF_CODE:
                return None
            if code in SIMPLE_COLORS:
                return {"kind": "simple", "code": code}
            return None

        if side == BOTH:
            left = _picked_for(LEFT)
            right = _picked_for(RIGHT)
            return left if left == right else None
        return _picked_for(side)

    # -- ours-vs-foreign discrimination ----------------------------------

    def _mark_sent(self, frame: bytes) -> None:
        # Composed commands are MULTI-frame groups; remember each frame
        # so echoes of any member are recognized as ours.
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
        and the held state is re-asserted periodically (see enforce_tick)
        whether that state is on or off.  OFF: revert to passive
        reflect-what-we-hear.  The flag is purely user-controlled.
        """
        if self.enforce != on:
            self.enforce = on
            self.resume()  # enforcement pauses any suspension
            self._notify()

    async def enforce_tick(self) -> None:
        """Periodic enforcement heartbeat, respecting the per-state cadence.

        Runs on the shared ENFORCE_INTERVAL_S ticker but only actually
        transmits when it is time for this pair:

        - enforced ON:  re-assert every ENFORCE_INTERVAL_S (hold color
          against demo/standalone drift),
        - enforced OFF: back off to ENFORCE_OFF_INTERVAL_S so we keep the
          ears out of demo mode without flogging the IR bus (keeps them
          dark by resetting the demo-entry timer before it fires).

        Always a single transmission (one pass, no extra repeats).  Only
        runs while the enforce switch is on.
        """
        if not self.enforce:
            return
        on = any(self.desired_on.values())
        interval = (
            ENFORCE_INTERVAL_S if on else ENFORCE_OFF_INTERVAL_S
        )
        if self._clock() - self._last_reassert_at < interval:
            return
        await self._send_held_state()
        self._last_reassert_at = self._clock()

    async def enforce_reassert(self) -> None:
        """Immediate re-assert, bypassing the cadence back-off.

        Called when a foreign (non-echo) MWM phrase is heard while
        enforcing: we can't tell what the ears were driven to, so pull our
        held state back now and restart the back-off timer.  No-op when not
        enforcing.
        """
        if not self.enforce:
            return
        await self._send_held_state()
        self._last_reassert_at = self._clock()

    # -- passive silence rule ---------------------------------------------

    def assume_off_if_silent(self, timeout_s: float) -> bool:
        """Reflect that the ears powered off after beacon silence.

        Applies ONLY when NOT enforcing and a light is on: if we have had no
        evidence the ears are alive for *timeout_s*, assume they powered off
        and turn the lights off.  Returns True if state changed (the caller
        should notify HA).  Enforcement mode never assumes off -- it always
        re-asserts its own state via enforce_reassert/enforce_tick.

        "Evidence" is the most recent of any beacon heard or any state
        command *we* sent.  Anchoring on the command (not just on a beacon)
        matters for a pair that has NEVER beamed since startup (ears dead or
        carried away): without it, `_last_beacon_at` stays 0 forever and such
        a room, once turned on, could never be assumed off again.
        """
        if self.enforce:
            return False
        if not any(self.desired_on.values()):
            return False
        last_evidence = max(self._last_beacon_at, self._last_command_at)
        if self._clock() - last_evidence < timeout_s:
            return False
        self.running_effect = None
        self.desired_on = {LEFT: False, RIGHT: False}
        self._notify()
        return True

    # -- commands ---------------------------------------------------------

    def _ear_color(self, side: str) -> tuple[str, int]:
        """Resolve one ear's effective color as ('simple'|'palette', value).

        A palette shade wins over any lingering simple code: applying a
        palette shade records it in ``palette_code`` and leaves ``codes``
        stale, so the palette entry is authoritative when present.
        """
        pp = self.palette_code.get(side)
        if pp is not None:
            return ("palette", pp & 0x7F)
        return ("simple", self.codes[side])

    def _record_color(self, side: str, pick: tuple[str, int]) -> None:
        """Record a color pick into the pair's held state (no transmission).

        ``pick`` is ``("simple", code)`` or ``("palette", index)``, with
        ``code == EAR_OFF_CODE`` meaning off.  Used by `apply_effect` to keep
        HA state matching the colors an effect was seeded with.
        """
        kind, value = pick
        if value == EAR_OFF_CODE:
            self.codes[side] = EAR_OFF_CODE
            self.palette_code[side] = None
            self.desired_on[side] = False
            return
        if kind == "palette":
            self.palette_code[side] = value
            return
        self.codes[side] = value
        self.palette_code[side] = None
        self.last_simple[side] = value
        self.desired_on[side] = True

    def _state_frames(self) -> list[bytes]:
        """Frames expressing the current pair state (see `_color_frames_for`)."""
        return self._color_frames_for()

    def _color_frames_for(
        self,
        left: tuple[str, int] | None = None,
        right: tuple[str, int] | None = None,
    ) -> list[bytes]:
        """Verified-form color frames for an explicit pair, or the current
        ear state when a side is omitted.

        Equal pairs use the canonical both-ears form (`90 6X` simple,
        `91 0E pp` palette).  When left and right differ, a single *fused*
        phrase sets each ear in one IR frame (samples/mwm-gwts-colors.tsv
        row ``left-blue-right-green-fused``; docs/mwm-show-protocol.md
        section 4, "Left vs right ears" and 12.13):

            ``91 6R 6L|08 ..``          simple right + simple left
            ``92 6R 0E L|80 ..``        simple right + palette left
            ``92 0E R 6L|08 ..``        palette right + simple left
            ``93 0E R 0E L|80 ..``      palette right + palette left

        A palette pick is ``("palette", index)``; a simple ``("simple",
        code)``, with code ``EAR_OFF_CODE`` meaning "off".  Multi-byte
        phrases run opcodes sequentially against both ears, so a both-ear
        opcode followed by a left-only opcode achieves per-side control in
        one burst.  This is what lets a right-ear color pick leave the left
        ear untouched in a single transmission (no right-only form exists;
        a left-ear pick uses its verified left-only form directly).
        """
        left = left or self._ear_color(LEFT)
        right = right or self._ear_color(RIGHT)
        if left == right:
            if left[0] == "palette":
                return [build_frame([0x0E, left[1]])]
            return [build_frame([left[1]])]
        lk, lv = left
        rk, rv = right
        if lk == "simple" and rk == "simple":
            return [build_frame([rv, LEFT_ONLY_BASE + lv - EAR_OFF_CODE])]
        if lk == "simple" and rk == "palette":
            return [build_frame([0x0E, rv, LEFT_ONLY_BASE + lv - EAR_OFF_CODE])]
        if lk == "palette" and rk == "simple":
            return [build_frame([rv, 0x0E, lv | 0x80])]
        return [build_frame([0x0E, rv, 0x0E, lv | 0x80])]

    async def _send_group(
        self, frames: list[bytes], repeat_count: int
    ) -> None:
        """Transmit frames as ONE logical command, repeated as a GROUP.

        Every pass runs the full sequence back-to-back and passes are sent
        immediately after one another -- the frames' built-in footers
        provide the inter-message spacing (no artificial pause between
        repeats, so rapid color-wheel browsing isn't held up). One failed
        pass logs and continues: partial IR still lands and the remaining
        passes heal it.
        """
        for attempt in range(repeat_count + 1):
            for frame in frames:
                try:
                    await self._transmit(frame, 0)
                    self._mark_sent(frame)
                    self._last_command_at = self._clock()
                    self._notify()
                except Exception:  # noqa: BLE001 - keep the group going
                    _LOGGER.exception("transmit failed (pass %d)", attempt)

    async def _send_state(self, repeat_count: int) -> None:
        """Send the composed current pair state.

        Also records the exact frames as the hold set, so enforcement can
        re-assert *what was actually asked for* rather than re-deriving
        from `codes` (which cannot express palette shades or effects).
        """
        self._hold_frames = self._state_frames()
        await self._send_group(self._hold_frames, repeat_count)

    async def _send_held_state(self) -> None:
        """Send whatever we are currently HOLDING for enforcement.

        Prefers the exact frames of the last command that drove the display
        (`_hold_frames` -- user pick or adopted foreign frame; can be a
        palette shade, an effect program, or a fused distinct-color pair).
        Falls back to `_state_frames()` when nothing has ever driven the
        display (fresh restart while enforcing defaults to all-off).
        """
        frames = self._hold_frames or self._state_frames()
        await self._send_group(frames, 0)

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
        ONLY the left slot changes we send the single left-only form:
        no intermediate flash, the right ear never hears a thing.
        """
        old_right = self.codes[RIGHT]
        self.palette_code[side] = None
        self.running_effect = None
        self.codes[side] = code
        self.desired_on[side] = code != EAR_OFF_CODE
        if code != EAR_OFF_CODE:
            self.last_simple[side] = code
        self.resume()
        if (
            side == LEFT
            and old_right == self.codes[RIGHT]
            and old_right != code  # equal pairs use the canonical form
            and EAR_OFF_CODE <= code <= 0x67
        ):
            frames = [build_frame([LEFT_ONLY_BASE + code - EAR_OFF_CODE])]
            self._hold_frames = frames
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
        """Bare both-ears turn-on: restore the pair's remembered color."""
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
            # never invent ear colors the phrase did not state.
            pal = desc.get("palette")
            if pal:
                pp = pal["index"]
                if pal["scope"] == "left":
                    self.codes[LEFT] = EAR_OFF_CODE
                    self.palette_code[LEFT] = pp
                else:
                    self.codes = {LEFT: EAR_OFF_CODE, RIGHT: EAR_OFF_CODE}
                    self.palette_code = {LEFT: pp, RIGHT: pp}
                self.running_effect = None
            else:
                self.running_effect = desc["summary"]
                self.palette_code = {LEFT: None, RIGHT: None}
                self.desired_on = {LEFT: True, RIGHT: True}
            # The adopted phrase is exactly what drove the display; holding
            # it lets enforcement re-assert the foreign state faithfully if
            # this room is later put into enforce mode (re-anchors the hold
            # instead of re-blasting an earlier user command).
            self._hold_frames = [bytes(frame)]
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
            self.codes[LEFT] = (
                EAR_OFF_CODE if content[0] == 0x68
                else EAR_OFF_CODE + content[0] - LEFT_ONLY_BASE
            )
            self.palette_code[LEFT] = None
        elif (
            len(content) == 2 and content[0] == 0x0E
            and content[1] & 0x7F <= 0x1D
        ):
            pp = content[1] & 0x7F
            if content[1] & 0x80:
                slots = {LEFT: pp}
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
            # color choreography itself stays opaque to us.
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
            # Record the adopted frames as the hold (see the wand-command
            # branch above): enforcement re-asserts whatever most recently
            # drove the display, user command or foreign adoption.
            self._hold_frames = [bytes(frame)]
            self._notify()
        return changed

    async def apply_palette(self, index: int, side: str | None = None) -> None:
        """Apply a palette shade using the TSV short forms only.

        LEFT-only: `91 0E pp|80` (samples/mwm-gwts-colors.tsv). With no
        side (the both-ears entity) and for RIGHT picks we record the new
        state and let `_state_frames` compose a single fused frame.  The
        fused form is what lets a RIGHT palette pick leave the left ear
        untouched in one burst -- no [both -> restore] two-frame flash, and
        no degrading to a both-ears frame that clobbers a simple left ear.
        """
        target = side or "both"

        if target == LEFT:
            self.palette_code[LEFT] = index
            self.running_effect = None
            self.desired_on[LEFT] = True
            self.resume()
            frames = [build_frame([0x0E, index | 0x80])]
            self._hold_frames = frames
            await self._send_group(frames, BURST_REPEATS)
            return

        if target == BOTH:
            self.palette_code[LEFT] = index
            self.palette_code[RIGHT] = index
            self.desired_on = {LEFT: True, RIGHT: True}
        else:  # RIGHT wheel pick: change right, leave left exactly as it is
            self.palette_code[RIGHT] = index
            self.desired_on[RIGHT] = True
        self.running_effect = None
        self.resume()
        await self._send_state(BURST_REPEATS)

    async def apply_effect(
        self,
        index: int,
        label: str,
        left: tuple[str, int] | None = None,
        right: tuple[str, int] | None = None,
    ) -> None:
        """Run an effect program, seeded with the current (or given) color.

        Real show effects are sent WITHOUT the `24` reset that this method
        used to prepend -- park captures and rig tests (2026-09-02) show `24`
        blanks or halves the ears (e.g. yellow-left/blank-right) whereas a
        bare `48 XX` acts on the current color cleanly.  So an effect is
        issued as its own phrase (plus any required `58` cycle companion
        from mwm.EFFECT_COMPANION), FOLLOWED by the color frames.

        Order matters: the effect program is started first, THEN the color
        is re-issued so the running effect adopts it (rig: pulse then blue =
        both ears pulsing blue in unison).  Sending the color before the
        effect instead splits the ears (white/red) or is ignored -- exactly
        the "picking an event blanks the ears" failure this fixes.

        The ears' A-B-A' bundles do NOT carry colors: A and A' are the same
        phrase, B is a companion timing/parameter block for the effect
        (docs/mwm-show-protocol.md section 6) -- effects run on the ear's
        current palette. Re-issuing the current color here is an HA-native
        way to seed the effect, consistent with the show. The bare `48 XX`
        (plus any `58`/`D0` modifiers) is the triggered effect phrase; the
        `24`-led companion template is not required for a single home send.
        """
        # Capture the current colors BEFORE running_effect/palette_code are
        # cleared, so the effect can be seeded with what is on screen now.
        color_frames = self._color_frames_for(left=left, right=right)

        self.running_effect = label
        self.palette_code = {LEFT: None, RIGHT: None}
        self.desired_on = {LEFT: True, RIGHT: True}
        # If explicit picks were given (batch mode), record them as the pair's
        # colors so HA reflects what the effect adopted; the effect program
        # owns the display while running, but these are the held colors.
        if left is not None:
            self._record_color(LEFT, left)
        if right is not None:
            self._record_color(RIGHT, right)
        self.resume()

        content = [0x48, index]
        companion = EFFECT_COMPANION.get(index)
        if companion is not None:
            content += [0x58, companion]
        frames = [build_frame(content), *color_frames]
        self._hold_frames = frames
        await self._send_group(frames, BURST_REPEATS)

    async def apply_incantation(
        self,
        family: str,
        *,
        label: str,
        left: tuple[str, int] | None = None,
        right: tuple[str, int] | None = None,
        cycle_mod: int | None = None,
    ) -> None:
        """Run a verified show-command *incantation* -- a single fused phrase
        in the real corpus's shape (per-ear palette + simple other ear +
        cycle timer + effect invoke + closing D0 clause), not the separate
        effect-then-color frames of `apply_effect`.

        Families are the mined show phrases in _mwm/incant.py, each derived
        from specific park/hat frames (python/mwm/incant.py documents the
        source hex + occurrence counts):

          * ``pulse``   left-ear palette shade + right simple color,
                        ``58 F0`` + ``48 04`` + ``D0 45 83``.  ``left`` must
                        be a palette pick; ``right`` a simple pick.
          * ``strobe``  white (or `right`) solid + ``58 tt`` + ``48 84``
                        strobe-into-running-program.
          * ``fade``    fade-out countdown ``F? 48 85 58 tt`` (the park
                        workhorse, 1375 occurrences).
          * ``rotation`` color-rotation set-piece + ~1 s delayed fade-out.

        The fused phrase is the honest single-burst reproduction of what
        park/wand/hat controllers transmit, so a home show that wants to
        look like the real thing sends these.  `cycle_mod` adds the
        ``D0 42 tt`` pulse cycle-rate clause.
        """
        family = family.lower()
        if family == "pulse":
            if left is None or left[0] != "palette":
                raise ValueError("incantation 'pulse' needs a palette left ear")
            right_simple = right[1] if right else self.codes[RIGHT]
            frames = [build_pulse(left[1], right_simple, cycle_mod=cycle_mod)]
            label = label or effect_label(0x04)
        elif family == "strobe":
            color = right[1] if right else DEFAULT_COLOR_CODE
            frames = [build_strobe(color)]
            label = label or effect_label(0x84)
        elif family == "fade":
            frames = [build_fade()]
            label = label or effect_label(0x85)
        elif family == "rotation":
            color = right[1] if right else DEFAULT_COLOR_CODE
            frames = [rotation_phrase(color)]
            label = label or effect_label(0x11)
        else:
            raise ValueError(f"unknown incantation '{family}'")

        self.running_effect = label
        if left is not None:
            self._record_color(LEFT, left)
        if right is not None:
            self._record_color(RIGHT, right)
        self.desired_on = {LEFT: True, RIGHT: True}
        self.resume()
        self._hold_frames = frames
        await self._send_group(frames, BURST_REPEATS)

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

        Routes through _send_state, so the other ear keeps its color via
        the both+left-only composition.  Already-dark ears are a no-op
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
        if side == LEFT:
            # Left-only OFF is a verified single form (`90 68`): the right
            # ear stays untouched -- no compose flash.
            frames = [build_frame([LEFT_ONLY_BASE])]
            self._hold_frames = frames
            await self._send_group(frames, BURST_REPEATS)
            return
        await self._send_state(BURST_REPEATS)

    async def apply_state(
        self,
        left: tuple[str, int] | None = None,
        right: tuple[str, int] | None = None,
        *,
        both: tuple[str, int] | None = None,
        effect: tuple[int, str] | None = None,
    ) -> None:
        """Native pair-level state: set both ears' colors + effect in one call.

        Automation-facing (mwm_ears.set_state), richer than light.turn_on:
        each ear can take a different exact color, or one pick can address
        both ears in the protocol's native both form, with a room-wide
        effect applied last.  Each color arg is an already-parsed pick --
        ``("simple", code)`` or ``("palette", index)``, with code/index of
        EAR_OFF_CODE meaning "off".  ``both`` is the both-ears shortcut (a
        per-side ``left``/``right`` wins over it for that ear).

        Transmissions are minimized: identical picks collapse to one native
        both frame; per-ear picks reuse the fused-both/left-only forms so
        the other ear keeps its own color with no flash; off-ing an already
        dark ear is a no-op.  Nothing is transmitted when no field is set.
        """
        if both is not None:
            if left is None:
                left = both
            if right is None:
                right = both
        if effect is None and left is None and right is None:
            return

        if effect is not None:
            # Effect FIRST, then the colors -- the running effect adopts the
            # just-issued color (rig-verified: pulse then blue = both ears
            # pulsing blue).  Reverse order splits the ears or is ignored.
            await self.apply_effect(*effect, left=left, right=right)
            return

        if left is not None and right is not None:
            if left == right:
                await self._send_both_pick(left)
            else:
                await self._send_side_pick(LEFT, left)
                await self._send_side_pick(RIGHT, right)
        elif left is not None:
            await self._send_side_pick(LEFT, left)
        elif right is not None:
            await self._send_side_pick(RIGHT, right)

    async def _send_both_pick(self, pick: tuple[str, int]) -> None:
        """Send one pick to both ears in the protocol's native both frame."""
        kind, code = pick
        if code == EAR_OFF_CODE:
            await self.apply_simple_both(EAR_OFF_CODE)
        elif kind == "simple":
            await self.apply_simple_both(code)
        else:
            await self.apply_palette(code)

    async def _send_side_pick(self, side: str, pick: tuple[str, int]) -> None:
        """Send one pick to a single ear, leaving the other exactly as it is."""
        kind, code = pick
        if code == EAR_OFF_CODE:
            await self.turn_off_side(side)
        elif kind == "simple":
            await self.apply_simple(side, code)
        else:
            await self.apply_palette(code, side=side)



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
    ) -> set:
        """Feed decoded frames from ONE receiver into the shared view.

        Returns the set of enforcing pairs that heard an authoritative
        (non-echo) signal and should be immediately re-asserted by the
        caller (see __init__._make_signal_handler).

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
        # Enforcing pairs that heard an authoritative (non-echo) signal and
        # should be pulled back onto their held state immediately, rather
        # than waiting for the next enforce_tick.  Deduped so one receive
        # batch produces at most one re-assert per pair.
        reassert: set = set()
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
                    # The alive observations always update; they are what
                    # the sensor/snapshot report.  But we must NOT
                    # re-assert desired_on=True from a beacon heard right
                    # after our own command: that beacon was likely already
                    # in flight when the ears processed e.g. an "all off",
                    # so it would slide the entity straight back on.  Only
                    # beacons arriving AFTER the stale window (i.e. the
                    # ears genuinely kept/woke beaconing) flip desired_on.
                    # An ENFORCING pair is exempt from that flip entirely:
                    # the held command is the whole truth for it (they
                    # still re-assert below on every beacon), so a beacon-
                    # proven-live ear beneath a held OFF command must not
                    # slide the entity back ON.
                    now = self._clock()
                    # Record per-pair beacon liveness for the passive
                    # assume-off rule; scoping it to each pair stops one
                    # room keeping another alive when they share a hub.
                    pair._last_beacon_at = now
                    stale = (
                        now - pair._last_command_at
                        <= BEACON_STALE_AFTER_COMMAND_S
                    )
                    if not stale and not pair.enforce:
                        pair.desired_on = {LEFT: True, RIGHT: True}
                    # We never transmit beacons, so any beacon heard is
                    # foreign -- while enforcing, pull back onto our held
                    # state (the one true re-assert trigger is "not ours").
                    if pair.enforce:
                        reassert.add(pair)
                    # The effect/color observations report what the ears
                    # are *currently* doing (useful for diagnostics), but
                    # for an ENFORCING pair they must not clobber the held
                    # command: enforcement re-asserts its own state, so we
                    # only mirror beacon-observed effect/palette into the
                    # passive (non-enforcing) display state.
                    if not pair.enforce:
                        pair.running_effect = (
                            effect_label(demo_effect)
                            if demo_effect is not None
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
                    # Foreign command while enforcing: don't adopt it, but
                    # pull our held state back immediately.  (Our own echoes
                    # were filtered above by matches_recent.)
                    reassert.add(pair)
                    continue
                pair.adopt_decoded(frame)
                pair.suspend(f"foreign command: {desc['summary']}")
        if changed:
            self._notify()
        return reassert

    def snapshot(self) -> str:
        snap = self.tracker.snapshot()
        return snap if snap else "unknown"

    def check_beacon_timeout(self, timeout_s: float) -> bool:
        """Apply the passive assume-off rule to every non-enforcing pair.

        Returns True if any pair's displayed state changed (the caller
        should notify HA).  Enforcing pairs are skipped by
        assume_off_if_silent -- they always re-assert their own state
        rather than assuming off.
        """
        changed = False
        for pair in self.pairs:
            if pair.assume_off_if_silent(timeout_s):
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
