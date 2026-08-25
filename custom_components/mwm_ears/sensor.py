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

from .const import CONF_RECEIVER_ENTITY, DEVICE_ID, INTEGRATION_VERSION, DOMAIN, HUB_KEY
from .ears import ObservedHub, ReceiverData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    receiver: ReceiverData = runtime["rx"]  # set: receiver half configured
    hub: ObservedHub = hass.data[DOMAIN][HUB_KEY]
    if not entry.data.get(CONF_RECEIVER_ENTITY):
        return  # this instance has no receiver half
    async_add_entities(
        [
            MwmPhraseSensor(receiver, entry),
            MwmCompanionSensor(receiver, entry),
            MwmAssumedStateSensor(hub, entry),
            MwmMessageCountSensor(receiver, entry),
        ]
    )


class _ReceiverSensor(SensorEntity):
    """Base wiring: refresh whenever this entry ingests a message."""

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, DEVICE_ID)},
            name="MWM Ears",
            manufacturer="Disney (Made With Magic)",
            model="MWM/GWTS infrared room controller",
            sw_version=INTEGRATION_VERSION,
        )
        self._base_id = entry.entry_id
        self._room = entry.data["name"]
        self._ir_entity_id = entry.data[CONF_RECEIVER_ENTITY]

    def _bind(self, listeners: list) -> None:
        self._listeners = listeners

    async def async_added_to_hass(self) -> None:
        self._listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        try:
            self._listeners.remove(self.async_write_ha_state)
        except ValueError:
            pass


class MwmPhraseSensor(_ReceiverSensor):
    """Message A of the most recent capture (the command phrase itself).

    For non-bundle captures this is simply the whole capture.
    """

    _attr_icon = "mdi:message-text-lock-outline"

    def __init__(self, receiver: ReceiverData, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._receiver = receiver
        self._bind(receiver.listeners)

    @property
    def unique_id(self) -> str:
        return f"{self._base_id}-last-phrase"

    @property
    def name(self) -> str:
        return f"{self._room} Last Phrase"

    @property
    def native_value(self) -> str | None:
        return self._receiver.last_phrase_hex

    @property
    def extra_state_attributes(self) -> dict:
        receiver = self._receiver
        return {
            "source": self._ir_entity_id,
            "is_bundle": receiver.last_is_bundle,
            "interpretation": receiver.last_summary or None,
            "all_frames": receiver.last_frames_hex or None,
            "messages_seen": receiver.message_count,
            "invalid_frames": receiver.invalid_count,
            "signals_seen": receiver.signals_seen,
            "receiver_rebinds": receiver.rebinds,
            "last_signal": receiver.last_signal_debug,
        }


class MwmCompanionSensor(_ReceiverSensor):
    """Message B of the most recent A-B-A' bundle (parameter block).

    State is none unless the last capture actually was a bundle; A'
    duplicates A and is deliberately not reported.
    """

    _attr_icon = "mdi:tune-vertical"

    def __init__(self, receiver: ReceiverData, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._receiver = receiver
        self._bind(receiver.listeners)

    @property
    def unique_id(self) -> str:
        return f"{self._base_id}-last-companion"

    @property
    def name(self) -> str:
        return f"{self._room} Last Companion"

    @property
    def native_value(self) -> str | None:
        return self._receiver.last_companion_hex


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
    def name(self) -> str:
        return f"{self._room} Assumed Ear State"

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
    def name(self) -> str:
        return f"{self._room} Message Count"

    @property
    def native_value(self) -> int:
        return self._receiver.message_count
