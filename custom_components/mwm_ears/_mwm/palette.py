"""Ear color tables and nearest-color matching.

Simple one-bit colors (60-6F) are the saturated primaries; the mixed palette
(0E XX) holds 30 measured shades. RGB values come from the rig-verified table
samples/mwm-gwts-colors.tsv (oPossum's measurements for the palette).
"""

from __future__ import annotations

# code -> (name, rgb)
SIMPLE_COLORS: dict[int, tuple[str, tuple[int, int, int]]] = {
    0x61: ("blue", (0x00, 0x00, 0xFF)),
    0x62: ("green", (0x00, 0xFF, 0x00)),
    0x63: ("cyan", (0x00, 0xFF, 0xFF)),
    0x64: ("red", (0xFF, 0x00, 0x00)),
    0x65: ("magenta", (0xFF, 0x00, 0xFF)),
    0x66: ("yellow", (0xFF, 0xFF, 0x00)),
    0x67: ("white", (0xFF, 0xFF, 0xFF)),
}

SIMPLE_COLOR_CODES = {name: code for code, (name, _) in SIMPLE_COLORS.items()}
EAR_STATE_OFF = "off"

_PALETTE_RGB: list[tuple[str, tuple[int, int, int]]] = [
    ("sky", (0xAC, 0xFE, 0xFE)),      # 00
    ("azure", (0x1F, 0x90, 0xFE)),    # 01
    ("cobalt", (0x1F, 0x4D, 0xFE)),   # 02
    ("sapphire", (0x1F, 0x00, 0xFE)), # 03
    ("navy blue", (0x00, 0x00, 0xFE)),# 04
    ("lilac", (0xFF, 0xCB, 0xFE)),    # 05
    ("orchid", (0xAC, 0x4D, 0xFE)),   # 06
    ("violet", (0x61, 0x26, 0xFF)),   # 07
    ("purple", (0x77, 0x01, 0xAB)),   # 08
    ("pink", (0xFF, 0xAC, 0xFE)),     # 09
    ("bubblegum", (0xFF, 0x2C, 0xFF)),# 0A
    ("magenta", (0xFE, 0x0D, 0xFF)),  # 0B
    ("fuchsia", (0xFF, 0x00, 0xCA)),  # 0C
    ("rose", (0xFF, 0x00, 0x61)),     # 0D
    ("crimson", (0xFF, 0x00, 0x11)),  # 0E
    ("gold", (0xFF, 0xCB, 0x16)),     # 0F
    ("orange", (0xFF, 0x56, 0x0A)),   # 10
    ("amber", (0xFF, 0x77, 0x01)),    # 11
    ("yellow", (0xFF, 0xFF, 0x00)),   # 12
    ("coral", (0xFF, 0x44, 0x00)),    # 13
    ("scarlet", (0xFF, 0x11, 0x00)),  # 14
    ("red", (0xFF, 0x00, 0x00)),      # 15
    ("ice", (0x00, 0xFE, 0xFF)),      # 16
    ("mint", (0x00, 0xFE, 0x6B)),     # 17
    ("spring green", (0x00, 0xFE, 0x2C)),  # 18
    ("lime", (0x00, 0xFE, 0x00)),     # 19
    ("green", (0x01, 0xFF, 0x00)),    # 1A
    ("pale green", (0xDB, 0xFF, 0xCA)),  # 1B
    ("white", (0xFE, 0xFE, 0xFE)),    # 1C
    # 1D is off/black; excluded from color matching.
]

PALETTE: dict[int, tuple[str, tuple[int, int, int]]] = {
    index: name_rgb for index, name_rgb in enumerate(_PALETTE_RGB)
}


def _hsv(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    import colorsys

    r, g, b = (v / 255.0 for v in rgb)
    h, s_, v = colorsys.rgb_to_hsv(r, g, b)
    return h * 360.0, s_, v


def _snap_cost(
    target: tuple[int, int, int], cand: tuple[int, int, int]
) -> float:
    """Perceptual snap cost: HUE dominates, saturation next, value last.

    Plain RGB distance sent dark/muted requests to wildly different
    hues (brown -> orange, grey-blue -> cyan) because every reachable
    ear shade is bright and saturated. Users read wrong hue as "far
    away"; brightness differences are tolerated far more.
    """
    th, ts, tv = _hsv(target)
    ch, cs, cv = _hsv(cand)
    dh = abs(th - ch)
    if dh > 180.0:
        dh = 360.0 - dh
    hue_term = (dh / 180.0) ** 2 * 100.0
    # Hue is meaningless near the grey axis -- let saturation match rule.
    hue_weight = min(ts, cs)
    sat_term = (ts - cs) ** 2 * 30.0
    val_term = (tv - cv) ** 2 * 20.0
    return hue_weight * hue_term + sat_term + val_term


def nearest_entry(
    rgb: tuple[int, int, int]
) -> tuple[str, int]:
    """Snap an RGB triple to the closest representable ear color.

    Returns (kind, code) where kind is "simple" or "palette". Simple colors
    win ties because they can be set per-ear (coordinating the both-ears
    and right-only primitives).
    """
    # Prefer simple colors on cost ties (they are set per-ear via composed
    # primitives); code breaks any remaining tie deterministically.
    candidates: list[tuple[float, int, int]] = []
    for code, (_, ref) in SIMPLE_COLORS.items():
        candidates.append((_snap_cost(rgb, ref), 0, code))
    for index, (_, ref) in PALETTE.items():
        candidates.append((_snap_cost(rgb, ref), 1, index))
    preference, code = min(candidates)[1:]
    return ("simple" if preference == 0 else "palette"), code
