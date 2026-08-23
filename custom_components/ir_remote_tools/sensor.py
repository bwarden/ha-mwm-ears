"""Sensor platform: MWM diagnostics per IR receiver.

Three entities per receiver entry:
- last message: raw frame hex plus our decoded interpretation,
- assumed ear state: best-effort "what mode are the lights in" inference,
- message counter: traffic volume including invalid-frame accounting.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .ears import ReceiverData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    receiver: ReceiverData = hass.data[DOMAIN][entry.entry_id]
    base = entry.data["name"]
    async_add_entities(
        [
            MwmLastMessageSensor(receiver, entry),
            MwmAssumedStateSensor(receiver, entry, base),
            MwmMessageCountSensor(receiver, entry),
        ]
    )


class _ReceiverSensor(SensorEntity):
    """Base wiring: refresh whenever the receiver ingests a message."""

    _attr_should_poll = False

    def __init__(self, receiver: ReceiverData, entry: ConfigEntry) -> None:
        self._receiver = receiver
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data["name"],
            manufacturer="Tasmota / MWM rig",
            model="MWM infrared receiver",
        )
        self._base_id = entry.entry_id

    async def async_added_to_hass(self) -> None:
        self._receiver.listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        try:
            self._receiver.listeners.remove(self.async_write_ha_state)
        except ValueError:
            pass


class MwmLastMessageSensor(_ReceiverSensor):
    """Raw hex of the most recent MWM message and its interpretation."""

    _attr_entity_category = None  # primary payload, not hidden diagnostics
    _attr_icon = "mdi:message-text-lock-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._base_id}-last-message"

    @property
    def native_value(self) -> str | None:
        frames = self._receiver.last_frames_hex.split("+")
        return frames[0] if frames and frames[0] else None

    @property
    def extra_state_attributes(self) -> dict:
        receiver = self._receiver
        attrs: dict = {
            "frames": receiver.last_frames_hex or None,
            "interpretation": receiver.last_summary or None,
            "messages_seen": receiver.message_count,
            "invalid_frames": receiver.invalid_count,
        }
        if receiver.last_seen is not None:
            attrs["last_seen"] = receiver.last_seen.isoformat()
        return attrs


class MwmAssumedStateSensor(_ReceiverSensor):
    """Best-effort description of what nearby ears are doing."""

    _attr_entity_category = None
    _attr_icon = "mdi:lamp-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._base_id}-assumed-state"

    @property
    def native_value(self) -> str:
        snapshot = self._receiver.snapshot()
        return snapshot if snapshot else "unknown"

    @property
    def extra_state_attributes(self) -> dict:
        tracker = self._receiver.tracker
        return {
            "left": tracker.left,
            "right": tracker.right,
            "running_effect": tracker.effect,
            "caveat": (
                "Inferred from overheard commands; ears may have been "
                "reprogrammed by transmitters we did not hear."
            ),
        }


class MwmMessageCountSensor(_ReceiverSensor):
    """Total MWM messages observed since integration start."""

    _attr_entity_category = None
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "messages"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def unique_id(self) -> str:
        return f"{self._base_id}-message-count"

    @property
    def native_value(self) -> int:
        return self._receiver.message_count
