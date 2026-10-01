"""Switch platform: per-room "Enforce Ears" and S18 space compensation.

When ON, the integration takes sole control of the room's ears -- the
light-entity state is re-asserted every ENFORCE_INTERVAL_S and foreign
commands (wand / other transmitter) are overridden.  When OFF (the default),
the integration passively reflects whatever is heard, matching earlier
behavior.  See ears.py's enforcement section for the mode rules.

The second switch, "S18 Space Compensation", is only created for rooms whose
emitter was positively identified as a stock Tuya-firmware S18 IR blaster
(the firmware that compresses every space by a fixed offset).  Turning it OFF
reverts to the nominal grid, which is the escape hatch if a future firmware
revision stops needing the correction.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_EMITTER_ENTITY, INTEGRATION_VERSION, DOMAIN, HUB_KEY
from .ears import ENFORCE_INTERVAL_S, EarPairState, ObservedHub

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """The shared MWM Ears device every room's switches hang off."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"mwm_ears_{entry.entry_id}")},
        name="MWM Ears",
        manufacturer="Disney (Made With Magic)",
        model="MWM/GWTS infrared room controller",
        sw_version=INTEGRATION_VERSION,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    if not entry.data.get(CONF_EMITTER_ENTITY):
        return  # enforcement needs an emitter to re-assert state
    pair: EarPairState = runtime["pair"]
    hub: ObservedHub = hass.data[DOMAIN][HUB_KEY]
    entities = [MwmEnforceSwitch(pair, hub, entry)]
    # Only offer the compensation toggle where compensation was detected --
    # on every other emitter the switch would be a no-op lie.
    if runtime.get("space_comp_available_us", 0):
        entities.append(MwmS18CompensationSwitch(runtime, entry))
    async_add_entities(entities)


class MwmEnforceSwitch(SwitchEntity):
    """Master/enforce switch for one room's ear pair."""

    _attr_should_poll = False
    _attr_icon = "mdi:ear-hearing"

    def __init__(
        self, pair: EarPairState, hub: ObservedHub, entry: ConfigEntry
    ) -> None:
        self._pair = pair
        self._hub = hub
        self._attr_name = f"{entry.data['name']} Enforce Ears"
        self._attr_unique_id = f"{entry.entry_id}-enforce"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        self._pair.listeners.append(self.async_write_ha_state)
        self._hub.listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        for listeners in (self._pair.listeners, self._hub.listeners):
            try:
                listeners.remove(self.async_write_ha_state)
            except ValueError:
                pass

    @property
    def is_on(self) -> bool:
        return self._pair.enforce

    async def async_turn_on(self, **kwargs) -> None:
        self._pair.set_enforce(True)
        self.async_write_ha_state()
        # Re-assert immediately so enforcement takes effect now, not on the
        # next periodic timer tick.
        await self._pair.enforce_reassert()

    async def async_turn_off(self, **kwargs) -> None:
        self._pair.set_enforce(False)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "reassert_every_s": ENFORCE_INTERVAL_S,
            "room_state": self._hub.snapshot(),
        }


class MwmS18CompensationSwitch(SwitchEntity):
    """Enable/disable the space pre-stretch for a Tuya-firmware S18 emitter."""

    _attr_should_poll = False
    _attr_icon = "mdi:timer-cog"

    def __init__(self, runtime: dict, entry: ConfigEntry) -> None:
        self._runtime = runtime
        self._attr_name = f"{entry.data['name']} S18 Space Compensation"
        self._attr_unique_id = f"{entry.entry_id}-s18-comp"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool:
        return self._runtime["space_comp_us"] != 0

    async def async_turn_on(self, **kwargs) -> None:
        self._runtime["space_comp_us"] = self._runtime["space_comp_available_us"]
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        # 0 = send the nominal grid (no compensation).
        self._runtime["space_comp_us"] = 0
        self.async_write_ha_state()
