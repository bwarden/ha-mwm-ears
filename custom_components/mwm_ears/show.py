"""Replay ``.msh`` show scripts through a room's infrared emitter.

A ``.msh`` file is a human-editable beat list: one command per line, with
optional ``@ms`` offsets.  ``hex`` lines carry full frames verbatim, while
``cue``/``cascade`` lines carry a delay-led phrase that expands into a
countdown chain -- the ``F?`` bytes delay the ear by their low nibble x 100
ms, so the members pre-roll before the beat's ``@ms`` and the immediate
``20`` go copy lands exactly on it.

This mirrors ``tools/mwm-send.py``'s ``_parse_show_script`` /
``_plan_publish_times`` so a script replays on the wire the same way whether
it is driven from a capture rig over MQTT or from Home Assistant via the
``infrared`` entity platform (which also covers non-MQTT ESPHome
transmitters).  Only the verbs the generator and the sample corpus emit are
supported: ``hex``, ``cue hex``/``cascade hex`` (with an optional
``members`` clause).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from typing import Any

try:  # pragma: no cover - exercised implicitly by both import paths
    from ._mwm import build_frame, frame_complete, parse_frame_hex
except ImportError:  # standalone (tests load this module without a package)
    from mwm import build_frame, frame_complete, parse_frame_hex

MIN_GAP_MS = 30.0
END_RESET = build_frame([0x60])

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _hex_byte(token: str, what: str) -> int:
    if not _HEX_RE.match(token) or len(token) > 2:
        raise ValueError(f"{what} must be hex bytes (got {token!r})")
    return int(token, 16)


def _cascade_content(argstr: str) -> list[int]:
    """The delay-led phrase content of a ``cue hex``/``cascade hex`` spec.

    The lead byte (``content[0]``) is the cue's delay byte: the countdown
    member set derives from it.  Only the raw-hex form is accepted here --
    the generator writes ``cue hex`` lines, and the named ``cue fade ...``
    family would need the source-only phrase builders.
    """
    argstr = argstr.strip()
    if argstr.startswith("incant "):
        argstr = argstr[len("incant "):]
    if not argstr.startswith("hex "):
        raise ValueError(
            "only 'cue hex <bytes>' is supported (named cue families are not "
            "replayable here)"
        )
    tokens = argstr[len("hex "):].split()
    content = [_hex_byte(tok, "cue hex") for tok in tokens]
    if not content or not (0x20 == content[0] or 0xF0 <= content[0] <= 0xFF):
        raise ValueError("cue phrase does not lead with a delay byte")
    return content


def _build_frames(line: str) -> list[bytes] | None:
    """Full frames from a ``hex`` line (``+`` joins frames, spaces join bytes)."""
    verb, _, argstr = line.partition(" ")
    if verb != "hex":
        return None
    frames = parse_frame_hex(" ".join(argstr.split()))
    if not frames:
        return None
    return [frame_complete(f) or f for f in frames]


def _expand(line: str, lineno: int) -> tuple[list[bytes], list[float], bool]:
    verb, _, argstr = line.partition(" ")
    if verb in ("cue", "cascade"):
        members: list[int] | None = None
        if " members " in argstr:
            argstr, _, member_str = argstr.partition(" members ")
            members = [
                _hex_byte(tok, f"line {lineno}: {verb} members")
                for tok in member_str.split()
            ]
        content = _cascade_content(argstr)
        tail = content[1:]
        lead = content[0]
        if members is not None:
            delays = [lead, *members]
        else:
            delays = list(range(lead, 0xF0, -1)) + [0x20]
        ordered = sorted(delays, reverse=True)  # the 20 go copy transmits last
        frames = [build_frame([d, *tail]) for d in ordered]
        rel = [0.0 if d == 0x20 else -((d & 0x0F) * 100.0) for d in ordered]
        return frames, rel, True
    frames = _build_frames(line)
    if frames is None:
        raise ValueError(f"line {lineno}: unknown command: {line!r}")
    return frames, [0.0] * len(frames), False


def parse_show_script(text: str) -> list[dict[str, Any]]:
    """Parse a ``.msh`` script into beats with resolved absolute timings.

    Returns ``{"t_ms": int, "frames": [bytes], "rel_ms": [float],
    "cascade": bool}`` per beat, ordered as written.  A beat without a
    preceding ``@`` lands at the cumulative time of the beats before it.
    """
    beats: list[dict[str, Any]] = []
    pending_ms: int | None = None
    cum = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            tok, _, rest = line[1:].partition(" ")
            try:
                ms = int(tok.strip())
            except ValueError as err:
                raise ValueError(f"line {lineno}: bad @ms offset: {raw!r}") from err
            if not rest.strip():
                pending_ms = ms
                continue
            line = rest.strip()
            pending_ms = None
            explicit = ms
        else:
            explicit = pending_ms
            pending_ms = None
        frames, rel, cascade = _expand(line, lineno)
        if explicit is not None:
            cum = explicit
        beats.append({"t_ms": cum, "frames": frames, "rel_ms": rel,
                      "cascade": cascade})
    if pending_ms is not None:
        raise ValueError("dangling @ms offset with no following beat")
    return beats


def plan_show(
    beats: Iterable[dict[str, Any]],
    *,
    min_gap_ms: float = MIN_GAP_MS,
    end_reset: bool = True,
) -> list[tuple[float, bytes]]:
    """Flatten beats into the exact ``(t_ms, frame)`` publish schedule.

    Mirrors ``mwm-send``'s clamp: consecutive publishes are held to a
    ``min_gap_ms`` floor, and a pre-roll member the floor (or the show's
    start) pushes off its true ``@ms + rel`` slot is dropped as redundant --
    the cue's GO stays anchored and is never preempted.  With ``end_reset``
    an all-off frame is appended after the last publish.
    """
    out: list[tuple[float, bytes]] = []
    last = -1e300
    for beat in beats:
        t0 = float(beat["t_ms"])
        rel = beat.get("rel_ms") or [0.0] * len(beat["frames"])
        for i, frame in enumerate(beat["frames"]):
            rt = rel[i] if i < len(rel) else 0.0
            t = t0 + rt
            if t < last + min_gap_ms:
                t = last + min_gap_ms
            t = max(0.0, t)
            if rt < 0 and t != t0 + rt:
                continue
            last = t
            out.append((t, frame))
    if end_reset:
        out.append((last + min_gap_ms, END_RESET))
    return out


class ShowPlayer:
    """Plays a planned schedule on an injectable async ``send`` callback.

    ``send`` is awaited for each frame (no extra repeat gap: show frames are
    individually timed).  Sleep and monotonic clock are injectable so the
    schedule can be unit-tested without real time.
    """

    def __init__(
        self,
        send: Callable[[bytes], Any],
        *,
        sleep: Callable[[float], Any] | None = None,
        clock: Callable[[], float] | None = None,
        min_gap_ms: float = MIN_GAP_MS,
    ) -> None:
        self._send = send
        self._sleep = sleep or asyncio.sleep
        self._clock = clock
        self._min_gap_ms = float(min_gap_ms)
        self.playing = False
        self.name = ""
        self.listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    def _notify(self) -> None:
        for cb in list(self.listeners):
            cb()

    def start(
        self,
        schedule: list[tuple[float, bytes]],
        *,
        repeat: int = 1,
        gap_s: float = 0.0,
        name: str = "",
    ) -> None:
        """Begin playback (cancelling any current show); returns immediately."""
        self.stop()
        self.name = name
        self.playing = True
        self._task = asyncio.ensure_future(self._run(schedule, max(1, int(repeat)), gap_s))
        self._notify()

    async def _run(self, schedule, repeat: int, gap_s: float) -> None:
        try:
            for i in range(repeat):
                if i and gap_s:
                    await self._sleep(gap_s)
                await self._play_once(schedule)
        except asyncio.CancelledError:
            raise
        finally:
            if asyncio.current_task() is self._task:
                self.playing = False
                self._task = None
                self._notify()

    async def _play_once(self, schedule: list[tuple[float, bytes]]) -> None:
        start = self._now()
        floor = self._min_gap_ms / 1000.0
        last: float | None = None
        for t_ms, frame in schedule:
            target = start + t_ms / 1000.0
            if last is not None:
                target = max(target, last + floor)
            now = self._now()
            if now < target:
                await self._sleep(target - now)
            await self._send(frame)
            last = self._now()

    def stop(self) -> None:
        """Cancel playback immediately (safe to call when idle)."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        if self.playing:
            self.playing = False
            self._notify()


def seconds_of(schedule: Iterable[tuple[float, bytes]]) -> float:
    """Total wall-clock length of a schedule, in seconds."""
    last = 0.0
    for t_ms, _frame in schedule:
        last = max(last, t_ms)
    return last / 1000.0
