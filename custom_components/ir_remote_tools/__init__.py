"""The IR Remote Tools integration.

Each config entry registers one infrared endpoint on the MQTT rig:

- a *transmitter* (e.g. a Tasmota IR blaster) exposes two light entities --
  left ear and right ear -- that drive every MWM ("Made With Magic") ear in
  range as a paired set;
- a *receiver* (a Tasmota device with SetOption58 capturing MWM traffic)
  exposes diagnostic sensors showing the last decoded message and our best
  inference of what mode the ears are currently in.
"""

from __future__ import annotations

import asyncio
import json
import logging

import homeassistant.components.mqtt as mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_KIND,
    CONF_RECEIVE_TOPIC,
    CONF_TRANSMIT_TOPIC,
    DOMAIN,
    KIND_RECEIVER,
    KIND_TRANSMITTER,
)
from .ears import EarPairState, ReceiverData

_LOGGER = logging.getLogger(__name__)

PLATFORMS_BY_KIND = {
    KIND_TRANSMITTER: ["light"],
    KIND_RECEIVER: ["sensor"],
}


def _parse_tasmota_ir(payload: bytes | str) -> list[str]:
    """Extract MWM frame hex strings from a Tasmota RESULT payload.

    Returns a list of packed hex frames ('+' joins multi-frame decodes).
    """
    data = json.loads(payload)
    ir = data.get("IrReceived")
    if not isinstance(ir, dict):
        return []
    protocol = str(ir.get("Protocol", ""))
    if not protocol.upper().startswith("MWM"):
        return []
    raw = str(ir.get("Data", "")).strip()
    return [part for part in raw.split("+") if part]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    if entry.data[CONF_KIND] == KIND_TRANSMITTER:
        store = EarPairState(hass, entry.data[CONF_TRANSMIT_TOPIC])
        hass.data[DOMAIN][entry.entry_id] = store
    else:
        receiver = ReceiverData()
        unsub = await mqtt.async_subscribe(
            hass, entry.data[CONF_RECEIVE_TOPIC], _make_message_handler(receiver)
        )
        receiver.unsubscribers.append(unsub)
        hass.data[DOMAIN][entry.entry_id] = receiver

    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS_BY_KIND[entry.data[CONF_KIND]]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_BY_KIND[entry.data[CONF_KIND]]
    )
    if unload_ok:
        receiver = hass.data[DOMAIN].pop(entry.entry_id, None)
        if isinstance(receiver, ReceiverData):
            for unsub in receiver.unsubscribers:
                unsub()
    return unload_ok


@callback
def _make_message_handler(receiver: ReceiverData):
    """Build an MQTT callback feeding decoded frames to the tracker."""

    @callback
    def handler(msg) -> None:
        try:
            hex_frames = _parse_tasmota_ir(msg.payload)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.debug("unparsable MQTT payload on %s", msg.topic)
            return
        if not hex_frames:
            return
        receiver.ingest(hex_frames)

    return handler
