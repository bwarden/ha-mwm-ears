"""MWM ("Made With Magic" / Glow With The Show) IR protocol library.

Pure-Python port of the framing, encoding, and decoding rules documented in
docs/mwm-show-protocol.md. Vendored inside the integration so the component
is self-contained (no pip dependency); the Home Assistant platforms import
from here.
"""

from .palette import (
    EAR_STATE_OFF,
    PALETTE,
    SIMPLE_COLORS,
    SIMPLE_COLOR_CODES,
    nearest_entry,
)
from .protocol import (
    CARRIER_HZ,
    FOOTER_GAP_US,
    TICK_US,
    build_55aa,
    build_frame,
    crc8_dallas,
    frame_is_valid,
    irsend_payload,
    parse_frame_hex,
    raw_timings,
    timings_for_frame,
)
from .timings import decode_timings
from .command import MwmCommand
from .decode import (
    EFFECT_LABELS,
    EarStateTracker,
    describe_55aa,
    describe_content,
    describe_bundle,
    describe_frame,
    effect_label,
)

__all__ = [
    "CARRIER_HZ",
    "FOOTER_GAP_US",
    "TICK_US",
    "build_55aa",
    "build_frame",
    "crc8_dallas",
    "frame_is_valid",
    "irsend_payload",
    "parse_frame_hex",
    "raw_timings",
    "timings_for_frame",
    "decode_timings",
    "MwmCommand",
    "PALETTE",
    "SIMPLE_COLORS",
    "SIMPLE_COLOR_CODES",
    "EAR_STATE_OFF",
    "nearest_entry",
    "EFFECT_LABELS",
    "EarStateTracker",
    "describe_55aa",
    "describe_content",
    "describe_bundle",
    "describe_frame",
    "effect_label",
]
