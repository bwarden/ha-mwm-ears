"""Shared runtime state bridging HA platforms to the vendored mwm library."""

from __future__ import annotations

import logging

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant

from ._mwm import (
    EarStateTracker,
    build_frame,
    describe_frame,
    effect_label,
    frame_is_valid,
    irsend_payload,
)

_LOGGER = logging.getLogger(__name__)

LEFT = "left"
RIGHT = "right"

EAR_OFF_CODE = 0x60

# Rig-verified palette template (samples/mwm-gwts-colors.tsv rows
# "palette-XX-both"): sets palette color pp on both ears simultaneously.
_PALETTE_TEMPLATE = [0x19, 0x07, 0x0F, 0x16]
_PALETTE_TEMPLATE_TAIL = [0x18, 0x04]


async def _publish(hass: HomeAssistant, topic: str, frame: bytes) -> None:
    """Transmit one MWM frame through the MQTT broker as a Tasmota IRsend."""
    payload = irsend_payload(frame)
    await hass.services.async_call(
        "mqtt",
        "publish",
        {"topic": topic, "payload": payload},
        blocking=True,
    )
    _LOGGER.debug("transmitted %s to %s", frame.hex().upper(), topic)


class EarPairState:
    """Desired per-ear colors for one transmitter's paired entities.

    Simple colors are transmitted as fused one-bit phrases (91 cL cR) so the
    two light entities stay independent. Palette shades have no verified
    per-ear phrase: the rig-verified template applies them to both ears at
    once, so choosing a snapped palette shade changes the partner ear too.
    """

    def __init__(self, hass: HomeAssistant, topic: str) -> None:
        self.hass = hass
        self.topic = topic
        self.codes: dict[str, int] = {LEFT: EAR_OFF_CODE, RIGHT: EAR_OFF_CODE}
        self.palette_index: int | None = None
        self.running_effect: str | None = None

    @property
    def palette_applied(self) -> bool:
        return self.palette_index is not None

    def _fused_frame(self) -> bytes:
        return build_frame([self.codes[LEFT], self.codes[RIGHT]])

    async def async_set_simple(self, side: str, code: int) -> None:
        self.palette_index = None
        self.running_effect = None
        self.codes[side] = code
        await _publish(self.hass, self.topic, self._fused_frame())

    async def async_set_palette(self, index: int) -> None:
        self.palette_index = index
        self.running_effect = None
        await _publish(
            self.hass,
            self.topic,
            build_frame(_PALETTE_TEMPLATE + [index] + _PALETTE_TEMPLATE_TAIL),
        )

    async def async_invoke_effect(self, index: int, label: str) -> None:
        # 24 lets an invocation take effect while a built-in program runs;
        # park captures show bare 48 XX phrases working as well.
        self.running_effect = label
        await _publish(self.hass, self.topic, build_frame([0x24, 0x48, index]))


class ReceiverData:
    """Decoded-message state shared by one receiver entry's sensors."""

    def __init__(self) -> None:
        self.tracker = EarStateTracker()
        self.unsubscribers: list = []
        self.listeners: list = []
        self.message_count = 0
        self.invalid_count = 0
        self.last_frames_hex = ""
        self.last_summary = ""
        self.last_seen = None

    def ingest(self, hex_parts: list[str]) -> None:
        """Feed '+'-joined frame hex strings from one MQTT message."""
        for part in hex_parts:
            ok, _ = frame_is_valid(part)
            if not ok:
                self.invalid_count += 1
                continue
            desc = describe_frame(part)
            # feed_frame re-validates; call it for state tracking only on
            # valid input so invalid traffic never perturbs assumptions.
            self.tracker.feed_frame(part)
            self.last_summary = (
                f"[{desc['kind']}] {desc['summary']}"
                if desc["kind"] != "beacon"
                else desc["summary"]
            )
        self.message_count += len(hex_parts)
        self.last_frames_hex = "+".join(p.upper() for p in hex_parts)
        self.last_seen = dt_util.utcnow()
        for callback in self.listeners:
            callback()

    def snapshot(self) -> str:
        return self.tracker.snapshot()
