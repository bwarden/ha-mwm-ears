"""Decode MWM show messages into human-readable descriptions.

Implements the instruction-set tables from docs/mwm-show-protocol.md section
4 plus the observed phrase templates (section 5): idle/demo beacons, static
color commands, effect invocations, timers/modifiers, group addressing, FEC
countdowns, and 55 AA system messages.
"""

from __future__ import annotations

from .palette import EAR_STATE_OFF, PALETTE, SIMPLE_COLORS

# docs/mwm-show-protocol.md section 4, effects table ([T]hread/[P]ark
# verified entries only).
EFFECT_LABELS: dict[int, str] = {
    0x00: "random effect",
    0x01: "quick smooth fade out",
    0x02: "fade through current color to black",
    0x03: "slow even pulse",
    0x04: "pulse",
    0x08: "quick four-color rotation",
    0x0D: "color sequence",
    0x0F: "flashing sequence",
    0x10: "quick flashing on one ear",
    0x11: "color rotation",
    0x13: "color sequence",
    0x1A: "power-on blinks and fade",
    0x1F: "off",
    0x80: "power-on display (demo mode entry)",
    0x81: "power-off display sequence",
    0x82: "hard color transitions",
    0x83: "crossfading transitions",
    0x84: "strobe flashes into running program",
    0x85: "fade out",
    0x86: "fade up",
}


def effect_label(index: int) -> str:
    """Human name for an invoked effect index, labelled or not."""
    if index in EFFECT_LABELS:
        return EFFECT_LABELS[index]
    if 0x87 <= index <= 0x8F:
        return f"color sequence {index:#04x}"
    return f"effect {index:#04x}"


def _color_name(simple_code: int) -> str:
    return SIMPLE_COLORS[simple_code][0] if simple_code in SIMPLE_COLORS else "off"


def _palette_desc(code: int) -> str:
    high = bool(code & 0x80)
    masked = code & 0x7F
    base = "off" if masked == 0x1D else (
        PALETTE[masked][0] if masked in PALETTE else f"palette[{masked:#04x}]"
    )
    return f"{base}{', per-ear register' if high else ''}"


def _walk_tokens(content: list[int] | bytes) -> tuple[list[str], int | None]:
    """Tokenize a phrase body; returns (tokens, last_invoked_effect)."""
    tokens: list[str] = []
    effect: int | None = None
    i, n = 0, len(content)

    def push(text: str) -> None:
        tokens.append(text)

    while i < n:
        b = content[i]

        if b == 0x24:
            push("reset/override")
            i += 1
        elif b == 0x20:
            push("start immediately")
            i += 1
        elif b == 0x25:
            push("stop motion")
            i += 1
        elif b == 0x26:
            push("close group range")
            i += 1

        elif b in (0x60, 0x68):
            push("both ears off" if b == 0x60 else "single ear off")
            i += 1
        elif 0x61 <= b <= 0x67:
            push(f"both ears {_color_name(b)}")
            i += 1
        elif 0x69 <= b <= 0x6F:
            push(f"single ear {_color_name(b)} (side unverified)")
            i += 1

        elif b == 0x0E and i + 1 < n:
            push(f"palette color {_palette_desc(content[i + 1])}")
            i += 2

        elif b == 0x48 and i + 1 < n:
            effect = content[i + 1]
            push(f"invoke {effect_label(effect)}")
            i += 2

        elif b == 0x58 and i + 1 < n:
            push(f"cycle timer {content[i + 1]:#04x} (~100 ms/count)")
            i += 2
        elif b == 0x59 and i + 2 < n:
            push(f"timer 200 ms granularity ({content[i + 1]:#04x},{content[i + 2]:#04x})")
            i += 3
        elif b == 0x5A and i + 3 < n:
            a, bb, c = content[i + 1 : i + 4]
            extra = f", period={a * 400 / bb:.1f}s" if c == 0x55 else ""
            push(f"fractional period ({a:#04x},{bb:#04x},{c:#04x}{extra})")
            i += 4
        elif b == 0x5B and i + 4 < n:
            push("four-arg group-select timer")
            i += 5
        elif b == 0xD0 and i + 2 < n:
            push(f"D0 modifier ({content[i + 1]:#04x},{content[i + 2]:#04x})")
            i += 3
        elif b == 0xD1 and i + 3 < n:
            push(
                "D1 modifier "
                f"({content[i + 1]:#04x},{content[i + 2]:#04x},{content[i + 3]:#04x})"
            )
            i += 4
        elif b == 0xD2 and i + 4 < n:
            push(
                "D2 modifier ("
                f"{content[i + 1]:#04x},{content[i + 2]:#04x},"
                f"{content[i + 3]:#04x},{content[i + 4]:#04x})"
            )
            i += 5

        elif 0xF1 <= b <= 0xFF:
            push(f"delay ~{(b - 0xF0) * 100} ms")
            i += 1
        elif b == 0xF0:
            push("F0 pulse-effect modifier")
            i += 1
        elif 0xF2 <= b <= 0xFD:
            # Countdown copies FD..F1 prefix a retransmission run; F2 overlaps
            # the delay table, so classify by position instead -- treat as
            # countdown when seen among other FEC markers.
            push(f"FEC countdown copy {b:#04x}")
            i += 1

        elif b == 0x0C and i + 1 < n:
            push(f"clock tick {content[i + 1]:#04x}")
            i += 2

        elif b == 0xA0 and i + 1 < n:
            push(f"group range bound {content[i + 1]:#04x}")
            i += 2
        elif b in (0x81, 0x89, 0x8C):
            push("group picker")
            i += 1

        elif b == 0x42:
            # Device state/id block opening every observed beacon.
            push("device state block (beacon)")
            i += 1

        else:
            push(f"unknown {b:#04x}")
            i += 1

    return tokens, effect


def _is_beacon(content: list[int] | bytes) -> bool:
    """Idle/demo beacon: `42 00 00 48 ss 0C t [D0 0E ?]`.

    The 7-byte park variant lacks the D0 modifier clause; the 10-byte live
    variant carries it. Byte 6 (the clock tick) varies freely.
    """
    body = list(content)
    if len(body) == 7:
        return (
            body[:4] == [0x42, 0x00, 0x00, 0x48] and body[5] == 0x0C
        )
    if len(body) == 10:
        return (
            body[:4] == [0x42, 0x00, 0x00, 0x48]
            and body[5] == 0x0C
            and body[7] == 0xD0
            and body[8] == 0x0E
        )
    return False


def describe_content(content: list[int] | bytes) -> dict:
    """Describe a phrase body (frame bytes between header and CRC)."""
    body = list(content)
    if _is_beacon(body):
        return {
            "kind": "beacon",
            "summary": (
                "idle beacon (demo effect running: "
                f"{effect_label(body[4])})"
            ),
            "demo_effect": body[4],
            "tokens": [],
            "effect": None,
        }
    tokens, effect = _walk_tokens(body)
    kind = (
        "effect-command" if effect is not None
        else "color-command" if any("ear" in t for t in tokens)
        else "command"
    )
    return {
        "kind": kind,
        "summary": "; ".join(tokens),
        "tokens": tokens,
        "effect": effect,
    }


def describe_55aa(data: bytes) -> dict:
    """Describe a 55 AA system message (full frame including checksum)."""
    payload = data[2:-1]
    games = {0: "demo cycle", 1: "blue solo", 2: "red memorisation",
             3: "yellow memorisation", 4: "laser tag"}
    if len(payload) >= 3 and payload[0] == 0x05 and payload[1] == 0x06:
        game = payload[2]
        name = games.get(game, f"game {game:#04x}")
        return {"kind": "55aa", "summary": f"interactive game: {name}"}
    if payload[:2] == b"\x08\xc4":
        return {"kind": "55aa", "summary": "ride shutdown command"}
    return {
        "kind": "55aa",
        "summary": "timecode/system broadcast (" + " ".join(
            f"{b:02X}" for b in payload[:8]
        ) + ("..." if len(payload) > 8 else "") + ")",
    }


def describe_bundle(frames: list[bytes]) -> dict | None:
    """Recognise an A-B-A' command bundle (doc section 3).

    Wands, paintbrushes and ears transmit every command three times:
    [phrase][companion][phrase]. First and third are identical; the middle
    one is a parameter block for the effect named by its embedded `48 XX`
    (cycle durations `58 tt`, scalers `D0 42 tt`, palette refs `0E xx`,
    clock writes `0C t`). Returns None unless the pattern matches exactly.
    """
    if len(frames) != 3 or frames[0] != frames[2] or frames[0] == frames[1]:
        return None
    phrase, companion = frames[0], frames[1]
    body = companion[1:-1]
    parts: list[str] = []
    i = 0
    while i < len(body) - 1:
        op = body[i]
        if op == 0x48:
            parts.append(f"effect: {effect_label(body[i + 1])}")
            i += 2
        elif op == 0x58:
            tt = body[i + 1]
            parts.append(
                "special cycle arg"
                if tt in (0xEE, 0xF0)
                else f"cycle ~{tt * 100} ms"
            )
            i += 2
        elif op == 0xD0 and i + 2 < len(body) and body[i + 1] == 0x42:
            parts.append(f"cycle scale {body[i + 2]} x 200 ms")
            i += 3
        elif op == 0x0C:
            parts.append(f"clock tick 0x{body[i + 1]:02X}")
            i += 2
        else:
            i += 1
    return {
        "kind": "bundle",
        "summary": (
            f"A-B-A' bundle: {describe_frame(phrase)['summary']} "
            f"[parameters: {'; '.join(parts) if parts else 'opaque companion'}]"
        ),
    }


def describe_frame(frame: str | bytes) -> dict:
    """Validate and describe one complete MWM frame (hex or bytes)."""
    from .protocol import frame_is_valid

    ok, reason = frame_is_valid(frame)
    if isinstance(frame, str):
        packed = bytes.fromhex(
            frame.replace(" ", "").replace("0x", "").replace("0X", "")
        )
    else:
        packed = bytes(frame)
    if not ok:
        return {"kind": "invalid", "summary": f"invalid frame: {reason}",
                "tokens": [], "effect": None}
    if len(packed) >= 3 and packed[0] == 0x55 and packed[1] == 0xAA:
        return describe_55aa(packed)
    return describe_content(packed[1:-1])


class EarStateTracker:
    """Best-effort inference of what paired ears are currently doing.

    Feeds decoded frames; tracks assumed per-ear colors and the running
    effect so diagnostic surfaces can answer "what mode are the lights in?".
    Beacon frames update the assumed running effect but never the colors,
    since beacons describe autonomous demo behaviour rather than commands.
    """

    def __init__(self) -> None:
        self._left: str = EAR_STATE_OFF
        self._right: str = EAR_STATE_OFF
        self._effect: str | None = None
        self.last_summary: str = ""

    @property
    def left(self) -> str:
        return self._left

    @property
    def right(self) -> str:
        return self._right

    @property
    def effect(self) -> str | None:
        return self._effect

    def feed_frame(self, frame: str | bytes) -> dict:
        desc = describe_frame(frame)
        self.last_summary = desc["summary"]
        if desc["kind"] == "invalid":
            return desc
        if desc["kind"] == "beacon":
            self._effect = effect_label(desc["demo_effect"])
            return desc

        if desc["kind"] == "55aa":
            return desc

        packed = bytes.fromhex(
            frame.replace(" ", "").replace("0x", "").replace("0X", "")
        ) if isinstance(frame, str) else bytes(frame)
        content = packed[1:-1]

        # Fused one-bit phrase: 91 cL cR sets each ear independently.
        if len(content) == 2 and packed[0] & 0x0F == 1 and (
            0x60 <= content[0] <= 0x67 and 0x60 <= content[1] <= 0x67
        ):
            self._left = _color_name(content[0]) if content[0] != 0x60 else EAR_STATE_OFF
            self._right = _color_name(content[1]) if content[1] != 0x60 else EAR_STATE_OFF
            self._effect = None
            return desc

        # Palette phrase template: 19 07 0F 16 pp 18 04 (rig-verified).
        if (
            len(content) == 7 and content[0] == 0x19 and content[4] & 0x7F <= 0x1D
            and content[5] == 0x18 and content[6] == 0x04
        ):
            pp = content[4] & 0x7F
            name = "off" if pp == 0x1D else (
                PALETTE.get(pp, ("unknown", None))[0]
            )
            self._left = self._right = name
            self._effect = None
            return desc

        # Bare both-ears color or blackout phrases.
        if any(t.startswith("both ears") for t in desc["tokens"]) or (
            len(content) == 1 and content[0] in (0x24, 0x60)
        ):
            for tok in desc["tokens"]:
                if tok.startswith("both ears"):
                    color = tok.removeprefix("both ears ")
                    self._left = self._right = color
                    self._effect = None
                    break
            else:
                if content[0] in (0x24, 0x60):
                    self._left = self._right = EAR_STATE_OFF
                    self._effect = None

        if desc["effect"] is not None:
            self._effect = effect_label(desc["effect"])

        return desc

    def snapshot(self) -> str:
        """One-line best-effort description of current ear state."""
        parts = []
        if self._left == self._right:
            parts.append(f"both ears {self._left}")
        else:
            parts.append(f"left {self._left}")
            parts.append(f"right {self._right}")
        if self._effect:
            parts.append(f"running: {self._effect}")
        return ", ".join(parts)
