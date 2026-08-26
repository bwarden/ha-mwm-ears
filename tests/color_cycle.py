#!/usr/bin/env python3
"""Offline colour/effect cycling test for MWM ear peripherals.

Sends every simple colour, palette shade, composite frame, and built-in
effect one at a time via MQTT (Tasmota IRsend), prompting for free-form
notes after each command.  Results are logged to a JSON file for later
analysis.

Requires: mosquitto_pub on PATH, MQTT config at
~/.config/ir-remote-tools/mqtt.json, and the _mwm library on sys.path.

Usage::

    python tests/color_cycle.py [--log FILE] [--mqtt-json PATH]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: make the _mwm package importable without HA or anything else.
# We import _mwm as a top-level package to avoid pulling in the HA
# integration __init__.py (which imports homeassistant).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_PYTHON = _HERE.parent  # python/
_MWM_SRC = _PYTHON / "custom_components" / "mwm_ears" / "_mwm"
if str(_MWM_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_MWM_SRC.parent))

# Import _mwm as a bare package (not through custom_components.mwm_ears).
import importlib  # noqa: E402
_mwm = importlib.import_module("_mwm")

PALETTE = _mwm.PALETTE
SIMPLE_COLORS = _mwm.SIMPLE_COLORS
build_frame = _mwm.build_frame
irsend_payload = _mwm.irsend_payload
parse_frame_hex = _mwm.parse_frame_hex
EFFECT_LABELS = _mwm.EFFECT_LABELS
effect_label = _mwm.effect_label

# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"


def _load_mqtt(path: Path) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _pub(mqtt: dict, payload: str) -> None:
    topic = mqtt["transmit"]
    cmd = [
        "mosquitto_pub",
        "-h", mqtt["broker"],
        "-p", str(mqtt["port"]),
        "-u", mqtt["username"],
        "-P", mqtt["password"],
        "-t", topic,
        "-m", payload,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _send_frame(mqtt: dict, frame: bytes) -> str:
    payload = irsend_payload(frame)
    _pub(mqtt, payload)
    return payload


def _send_hex(mqtt: dict, hex_str: str) -> str:
    """Send one or more '+'-joined hex frames; return combined payload."""
    parts = []
    for frame in parse_frame_hex(hex_str):
        parts.append(irsend_payload(frame))
        _pub(mqtt, irsend_payload(frame))
    return " + ".join(parts)


# ---------------------------------------------------------------------------
# Cycle sequence definition
# ---------------------------------------------------------------------------

# Reset frame: bare 0x24 (both ears off)
_RESET = build_frame([0x24])

# Simple colours: both ears (0x61..0x67)
_SIMPLE_BOTH: list[tuple[str, int]] = [
    (name, code) for code, (name, _) in sorted(SIMPLE_COLORS.items())
]

# Simple colours: right ear only (0x69..0x6F, offset from both-ears by 8)
_SIMPLE_RIGHT: list[tuple[str, int]] = [
    (name, 0x68 + (code - 0x60)) for code, (name, _) in sorted(SIMPLE_COLORS.items())
]

# Palette shades: both ears (0x00..0x1C, via 0x0E XX)
_PALETTE_BOTH: list[tuple[str, int]] = [
    (name, idx) for idx, (name, _) in sorted(PALETTE.items()) if idx <= 0x1C
]

# Palette shades: right ear only (0x80..0x9C, via 0x0E XX|80)
_PALETTE_RIGHT: list[tuple[str, int]] = [
    (name, idx | 0x80) for idx, (name, _) in sorted(PALETTE.items()) if idx <= 0x1C
]

# Composite / fused commands from the verified TSV
_COMPOSITE: list[tuple[str, str, str]] = [
    ("left-magenta-right-yellow", "90 65 99 + 90 6E B9",
     "set both magenta, then override right yellow"),
    ("left-blue-right-green-fused", "91 61 6A 06",
     "single fused frame: left blue, right green"),
    ("left-white-right-orange", "91 0E 00 5F + 91 0E 94 2F",
     "set both white, then override right orange"),
    ("left-white-right-orange-fused", "94 0E 00 0E 94 11",
     "fused 6-byte frame; may be ignored by strict decoders"),
]

# Built-in effects (0x48 XX)
_EFFECTS: list[tuple[str, int]] = [
    (effect_label(idx), idx) for idx in sorted(EFFECT_LABELS.keys())
]


def _build_cycle() -> list[dict]:
    """Assemble the full command sequence."""
    seq: list[dict] = []

    def add(kind: str, name: str, hex_data: str, desc: str = "") -> None:
        seq.append({"kind": kind, "name": name, "hex": hex_data, "desc": desc})

    def reset() -> None:
        add("reset", "both off", "24")

    # -- simple both-ears --
    for name, code in _SIMPLE_BOTH:
        add("simple_both", name, f"{code:02X}", f"simple {name}, both ears")
    reset()

    # -- simple right-only --
    for name, code in _SIMPLE_RIGHT:
        add("simple_right", f"{name} (right)", f"{code:02X}",
            f"simple {name}, right ear only")
    reset()

    # -- palette both-ears --
    for name, idx in _PALETTE_BOTH:
        add("palette_both", name, f"0E {idx:02X}",
            f"palette 0x{idx:02X} {name}, both ears")
    reset()

    # -- palette right-only --
    for name, idx in _PALETTE_RIGHT:
        add("palette_right", f"{name} (right)", f"0E {idx:02X}",
            f"palette 0x{idx & 0x7F:02X} {name}, right ear only")
    reset()

    # -- composite / fused --
    for name, hex_data, desc in _COMPOSITE:
        add("composite", name, hex_data, desc)
    reset()

    # -- effects --
    for name, idx in _EFFECTS:
        add("effect", name, f"48 {idx:02X}", f"effect {name} (0x{idx:02X})")
    reset()

    return seq


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    mqtt = _load_mqtt(Path(args.mqtt_json))
    cycle = _build_cycle()
    session_ts = datetime.now(timezone.utc).isoformat()
    log_path = Path(args.log) if args.log else Path(
        f"color_cycle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )

    print(f"MWM colour/effect cycle test")
    print(f"MQTT transmit topic: {mqtt['transmit']}")
    print(f"Logging to: {log_path}")
    print(f"Commands in cycle: {len(cycle)}")
    print(f"Press Enter to advance after each command; type a note + Enter to log it.")
    print()

    entries: list[dict] = []
    try:
        for i, cmd in enumerate(cycle, 1):
            hex_data = cmd["hex"]
            kind = cmd["kind"]

            # Build and send
            if kind == "reset":
                payload = _send_frame(mqtt, _RESET)
            elif kind in ("simple_both", "simple_right"):
                code = int(hex_data, 16)
                frame = build_frame([code])
                payload = _send_frame(mqtt, frame)
            elif kind == "palette_both":
                parts = [int(x, 16) for x in hex_data.split()]
                frame = build_frame(parts)
                payload = _send_frame(mqtt, frame)
            elif kind == "palette_right":
                parts = [int(x, 16) for x in hex_data.split()]
                frame = build_frame(parts)
                payload = _send_frame(mqtt, frame)
            elif kind == "effect":
                parts = [int(x, 16) for x in hex_data.split()]
                frame = build_frame(parts)
                payload = _send_frame(mqtt, frame)
            elif kind == "composite":
                payload = _send_hex(mqtt, hex_data)
            else:
                payload = hex_data

            # Display
            tag = f"[{i}/{len(cycle)}]"
            label = cmd["name"]
            desc = cmd.get("desc", "")
            print(f"{tag} {kind:14s} {label:28s} hex={hex_data}")
            if desc:
                print(f"    {desc}")

            # Prompt for notes
            try:
                note = input("    note> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nInterrupted.")
                break

            now = datetime.now(timezone.utc).isoformat()
            entries.append({
                "t": now,
                "kind": kind,
                "name": cmd["name"],
                "hex": hex_data,
                "payload": payload,
                "notes": note,
            })

            print()
    except KeyboardInterrupt:
        print("\nInterrupted.")

    # Write log
    log = {
        "session": session_ts,
        "mqtt_topic": mqtt["transmit"],
        "total_commands": len(entries),
        "entries": entries,
    }
    log_path.write_text(json.dumps(log, indent=2) + "\n")
    print(f"Logged {len(entries)} entries to {log_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MWM ear colour/effect cycling test",
    )
    parser.add_argument(
        "--log", default=None,
        help="Output JSON log file (default: color_cycle_YYYYMMDD_HHMMSS.json)",
    )
    parser.add_argument(
        "--mqtt-json", default=str(_DEFAULT_MQTT),
        help="Path to MQTT config JSON (default: ~/.config/ir-remote-tools/mqtt.json)",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
