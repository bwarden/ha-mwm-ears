"""Verified show-command incantations mined from the real captures.

PURPOSE
-------
The park / hat / wand corpus (samples/MRDF*, EMLG*, headband, measured by
tools/build_corpus.py + tools/analyze_shape.py) shows that real MWM show
controllers do NOT send a bare effect and a bare color.  They send single,
*fused* phrases: close-group + per-ear color(s) + cycle timer + effect
invocation + D0/D1 pacing modifiers, all in one frame.  This module encodes
the specific frame shapes that appear in the corpus so both the rig tool
(tools/mwm-send.py) and the Home Assistant integration can emit commands
built in the same verified shape instead of the naive `48 XX` + separate
color frame.

Each builder documents the corpus frame(s) it was derived from (hex + count
+ what describe_frame says).  Colors are supplied as protocol codes
(simple 0x61..0x67 via mwm.SIMPLE_COLOR_CODES, palette index 0x00..0x1C via
mwm.PALETTE).  Per-ear forms use the per-ear register bit (0x80 on the
palette byte) exactly as the corpus does -- left ear only; there is no
right-only single form in the wild corpus.

Requires: nothing beyond the protocol module this package already imports.
"""

from __future__ import annotations

from .protocol import EAR_OFF_CODE, LEFT_ONLY_BASE, build_frame

# The D0 (0x45, 0x83) clause that closes the park's per-color effect phrases.
D0_COLOR_EFFECT = 0xD0, 0x45, 0x83


def build_pulse(
    left_pp: int,
    right_simple: int,
    *,
    both_first: bool = False,
    cycle_mod: int | None = None,
) -> bytes:
    """Fused pulse phrase: per-ear palette + simple other ear + ``58 F0`` +
    ``48 04`` + closing ``D0 45 83``.

    ``left_pp`` is the palette index for the *left* ear (per-ear register
    bit ORed in), ``right_simple`` the simple-color code for the right ear
    (0x61..0x67).  ``both_first`` selects the other observed op ordering:
    simple-both first then per-ear palette.  ``cycle_mod`` adds the
    ``D0 42 tt`` cycle-rate clause between the invoke and the close.

    Derived from (both real park frames):
      * ``9B 96 26 0E 81 61 58 F0 48 04 D0 45 83`` (53x) -- palette sky
        blue, both blue, pulse.  ``both_first=False``, prefix 0x96.
      * ``9E 94 26 64 0E 8D 58 F0 48 04 D0 42 16 D0 45 83`` (11x) -- both
        red, left rose pink, pulse at ``D0 42 16``.  ``both_first=True``,
        prefix 0x94.
    """
    if both_first:
        content = [
            0x94, 0x26,
            right_simple,                 # both solid
            0x0E, left_pp | 0x80,         # per-ear palette register (left)
            0x58, 0xF0,                   # ~100 ms cycle timer (pulse needs 58 F0)
            0x48, 0x04,                   # invoke Pulse
        ]
    else:
        content = [
            0x96, 0x26,
            0x0E, left_pp | 0x80,         # per-ear palette register (left)
            right_simple,                 # both / other-ear solid
            0x58, 0xF0,
            0x48, 0x04,
        ]
    if cycle_mod is not None:
        content += [0xD0, 0x42, cycle_mod]
    content += list(D0_COLOR_EFFECT)
    return build_frame(content)


def build_strobe(
    color: int = 0x67,
    timer: int = 0x02,
    *,
    delay: int | None = 0xF1,
    reset: bool = True,
) -> bytes:
    """Fused strobe phrase: ``[delay] [reset] color ``58`` timer ``48 84``.

    “Strobe flashes into running program” is issued against a standing
    color, so the color rides inside the phrase (0x67 white in the corpus).
    ``delay`` is a ``F1``-``FF`` countdown byte or ``None`` to omit;
    ``reset`` toggles the leading ``24`` override.

    Derived from:
      * ``96 F1 24 67 58 02 48 84 ..`` (2x) -- delay 100 ms + reset, white,
        strobe.
      * ``95 20 67 58 01 48 84 ..`` (5x) -- immediate, white, timer 01,
        strobe (no reset).
    """
    content: list[int] = []
    if delay is not None:
        content.append(delay)
    if reset:
        content.append(0x24)
    content += [color, 0x58, timer, 0x48, 0x84]
    return build_frame(content)


def build_fade(cycle: int = 0x05, delay: int = 0xF2) -> bytes:
    """Fade-out countdown phrase: ``delay ``48 85`` ``58`` cycle``.

    The park workhorse (1375 occurrences across the corpus): ``F?`` delay
    then ``48 85`` fade-out stretched by ``58 tt``.  Delay 0x20 = immediate
    copy, which is the final FEC countdown member.

    Derived from:
      * ``94 F2 48 85 58 05 ..`` (16x) -- 200 ms delay, cycle 0x05.
      * ``94 20 48 85 58 05 ..`` (15x) -- immediate copy, cycle 0x05.
    """
    return build_frame([delay, 0x48, 0x85, 0x58, cycle])


def rotation_phrase(
    simple: int,
    cycle_mod: int = 0x01,
    *,
    left: int | None = None,
) -> bytes:
    """Color-rotation show phrase: reset + ``48 11`` + ``D0 3D tt`` +
    colors + ``FA`` + ``48 85``.

    The park's rotation set-piece rotates through the given simple color on
    both ears, then fades out ~1 s later.  ``left`` optionally gives the
    left-ear code separately (corpus uses the same color on both).

    Derived from:
      * ``9B F1 24 48 11 D0 3D 01 62 6A FA 48 85 ..`` (2x) -- both green /
        left green, rotation, fade out at ~1000 ms.
    """
    left_code = LEFT_ONLY_BASE + simple - EAR_OFF_CODE
    return build_frame([
        0xF1, 0x24,
        0x48, 0x11, 0xD0, 0x3D, cycle_mod,
        simple, left if left is not None else left_code,
        0xFA, 0x48, 0x85,
    ])