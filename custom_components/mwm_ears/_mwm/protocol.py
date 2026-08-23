"""MWM show-message framing and IR signal encoding.

Frame layout (docs/mwm-show-protocol.md section 2):

    +--------+---------------------------+--------+
    |  0x9L  | content (L+1 bytes)       | crc8   |
    +--------+---------------------------+--------+
    total length = L + 3 bytes

CRC-8/Dallas (poly 0x8C reflected, init 0). ``55 AA`` system messages use an
additive checksum over the payload (bytes after AA) mod 256 instead.

Signal: 2400 bps UART/IRDA-SIR over a 38 kHz carrier -- per byte a start mark
tick, eight data bits LSB-first with space=1, a stop space tick; equal
adjacent levels merge into single runs; the message ends with the ~30 ms
inter-command gap. This mirrors web/src/lib/protocol/mwm.ts toPronto().
"""

from __future__ import annotations

TICK_US = 417
FOOTER_GAP_US = 30000
CARRIER_HZ = 38000

MAX_CONTENT = 16


def crc8_dallas(data: bytes | list[int] | tuple[int, ...]) -> int:
    """CRC-8/Dallas (polynomial 0x8C reflected, init 0x00)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc & 0xFF


def build_frame(content: list[int]) -> bytes:
    """Build one show message from its content bytes (header + CRC added)."""
    n = len(content)
    if not 1 <= n <= MAX_CONTENT:
        raise ValueError(f"content must be 1..{MAX_CONTENT} bytes, got {n}")
    frame = [0x90 | (n - 1), *content]
    frame.append(crc8_dallas(frame))
    return bytes(frame)


def build_55aa(payload: list[int], prefix: list[int] | None = None) -> bytes:
    """Build a 55 AA system message with the additive payload checksum."""
    cs = sum(payload) % 256
    return bytes([0x55, 0xAA, *payload, cs])


def _normalize(hex_or_bytes: str | bytes) -> bytes:
    if isinstance(hex_or_bytes, bytes):
        return hex_or_bytes
    packed = hex_or_bytes.replace(" ", "").replace("0x", "").replace("0X", "")
    if len(packed) % 2:
        raise ValueError("odd number of hex digits")
    try:
        return bytes.fromhex(packed)
    except ValueError as err:
        raise ValueError(f"non-hex input: {hex_or_bytes!r}") from err


def frame_is_valid(hex_or_bytes: str | bytes) -> tuple[bool, str]:
    """Validate framing; returns (ok, reason).

    Accepts spaced or packed hex. Show messages must satisfy the length rule
    (low nibble of byte 0 == total - 3) and CRC-8/Dallas. 55 AA system
    messages validate via the additive payload checksum.
    """
    try:
        data = _normalize(hex_or_bytes)
    except ValueError as err:
        return False, str(err)
    if not data:
        return False, "empty"
    if len(data) >= 3 and data[0] == 0x55 and data[1] == 0xAA:
        want = sum(data[2:-1]) % 256
        if data[-1] != want:
            return False, f"additive checksum mismatch: got {data[-1]:02X} want {want:02X}"
        return True, ""
    hdr = data[0]
    if (hdr & 0xF0) != 0x90:
        return False, f"header {hdr:02X} is not 0x9x"
    length_nibble = hdr & 0x0F
    if length_nibble != len(data) - 3:
        return False, f"length rule violated: L={length_nibble} but total={len(data)}"
    want = crc8_dallas(data[:-1])
    if data[-1] != want:
        return False, f"CRC mismatch: got {data[-1]:02X} want {want:02X}"
    return True, ""


def timings_for_frame(frame: bytes) -> list[int]:
    """Absolute-duration burst sequence (alternating mark/space, us).

    Starts with a mark, ends with the inter-command gap space.
    """
    flat: list[int] = []
    for byte in frame:
        flat.append(TICK_US)  # start bit: mark
        for i in range(8):
            bit = (byte >> i) & 1
            flat.append(-TICK_US if bit else TICK_US)  # space = 1
        flat.append(-TICK_US)  # stop bit: space
    flat.append(-FOOTER_GAP_US)

    merged: list[int] = []
    for value in flat:
        if merged and (merged[-1] < 0) == (value < 0):
            merged[-1] += value
        else:
            merged.append(value)
    return [abs(v) for v in merged]


def irsend_payload(frame: bytes) -> str:
    """Tasmota IRsend raw command payload for transmitting this frame."""
    timings = timings_for_frame(frame)
    return ",".join([str(CARRIER_HZ), *(str(t) for t in timings)])


def raw_timings(frame: bytes) -> list[int]:
    """Signed-microsecond runs in the HA infrared framework convention.

    Positive values are marks (carrier on), negative values spaces,
    strictly alternating, ending with the inter-command gap space. This is
    the format expected by InfraredCommand.get_raw_timings().
    """
    return [
        value if index % 2 == 0 else -value
        for index, value in enumerate(timings_for_frame(frame))
    ]


def parse_frame_hex(text: str) -> list[bytes]:
    """Split a '+'-joined hex sequence into individual frames."""
    frames = []
    for part in text.split("+"):
        packed = part.strip().replace(" ", "").replace("0x", "").replace("0X", "")
        if packed:
            frames.append(bytes.fromhex(packed))
    return frames
