"""Sensor platform: MWM diagnostics bound to an infrared receiver.

Three entities per receiver entry:
- last message: raw frame hex plus our decoded interpretation,
- assumed ear state: room-level best-effort "what mode are the lights in"
  inference across every transmitter's overheard traffic,
- message counter including invalid-frame accounting.
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

from .const import CONF_ENTITY_ID, DOMAIN, HUB_KEY
from .ears import ObservedHub, ReceiverData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    receiver: ReceiverData = hass.data[DOMAIN][entry.entry_id]
    hub: ObservedHub = hass.data[DOMAIN][HUB_KEY]
    async_add_entities(
        [
            MwmLastMessageSensor(receiver, entry),
            MwmAssumedStateSensor(hub, entry),
            MwmMessageCountSensor(receiver, entry),
        ]
    )


class _ReceiverSensor(SensorEntity):
    """Base wiring: refresh whenever this entry ingests a message."""

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data["name"],
            manufacturer="MWM / Glow With The Show",
            model="MWM infrared receiver binding",
        )
        self._base_id = entry.entry_id
        self._ir_entity_id = entry.data[CONF_ENTITY_ID]

    def _bind(self, listeners: list) -> None:
        self._listeners = listeners

    async def async_added_to_hass(self) -> None:
        self._listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        try:
            self._listeners.remove(self.async_write_ha_state)
        except ValueError:
            pass


class MwmLastMessageSensor(_ReceiverSensor):
    """Raw hex of the most recent MWM message and its interpretation."""

    _attr_icon = "mdi:message-text-lock-outline"

    def __init__(self, receiver: ReceiverData, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._receiver = receiver
        self._bind(receiver.listeners)

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
        return {
            "source": self._ir_entity_id,
            "frames": receiver.last_frames_hex or None,
            "interpretation": receiver.last_summary or None,
            "messages_seen": receiver.message_count,
            "invalid_frames": receiver.invalid_count,
        }


class MwmAssumedStateSensor(_ReceiverSensor):
    """Best-effort description of what nearby ears are doing.

    Reads the shared hub so the answer covers the whole room (any receiver
    can hear any transmitter), not just this one capture point.
    """

    _attr_icon = "mdi:lamp-outline"

    def __init__(self, hub: ObservedHub, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._hub = hub
        self._bind(hub.listeners)

    @property
    def unique_id(self) -> str:
        return f"{self._base_id}-assumed-state"

    @property
    def native_value(self) -> str:
        return self._hub.snapshot()

    @property
    def extra_state_attributes(self) -> dict:
        tracker = self._hub.tracker
        return {
            "left": tracker.left,
            "right": tracker.right,
            "running_effect": tracker.effect,
            "last_foreign_command": self._hub.last_foreign_summary or None,
            "caveat": (
                "Inferred from overheard commands; ears may have been "
                "reprogrammed by transmitters we did not hear."
            ),
        }


class MwmMessageCountSensor(_ReceiverSensor):
    """Total MWM messages observed since integration start."""

    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "messages"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, receiver: ReceiverData, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._receiver = receiver
        self._bind(receiver.listeners)

    @property
    def unique_id(self) -> str:
        return f"{self._base_id}-message-count"

    @property
    def native_value(self) -> int:
        return self._receiver.message_count
