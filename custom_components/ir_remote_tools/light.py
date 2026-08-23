"""Light platform: left/right MWM ear lights per IR transmitter."""

from __future__ import annotations

import logging

from homeassistant.components.light import (
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util.color import color_hs_to_RGB

from .const import DOMAIN
from .ears import LEFT, RIGHT, EarPairState
from ._mwm import nearest_entry

_LOGGER = logging.getLogger(__name__)

# Curated effect catalog (docs/mwm-show-protocol.md section 4): labels shown
# in HA mapped to stored effect program indices.
LIGHT_EFFECTS: dict[str, int] = {
    "Fade out": 0x85,
    "Fade up": 0x86,
    "Slow even pulse": 0x03,
    "Pulse": 0x04,
    "Strobe flash": 0x84,
    "Hard transitions": 0x82,
    "Crossfade transitions": 0x83,
    "Color rotation": 0x11,
    "Flashing sequence": 0x0F,
    "Quick four-color rotation": 0x08,
    "Random effect": 0x00,
    "Blackout": 0x1F,
}

_SIDE_NAMES = {LEFT: "Left", RIGHT: "Right"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    store: EarPairState = hass.data[DOMAIN][entry.entry_id]
    name = entry.data["name"]
    async_add_entities(
        [
            MwmEarLight(store, entry, LEFT),
            MwmEarLight(store, entry, RIGHT),
        ]
    )


class MwmEarLight(LightEntity):
    """One ear of the pair driven by a transmitter.

    Both entities control the same physical room of ears; the fused-phrase
    path keeps their simple colors independent. Any HS colour picked in HA
    snaps to the nearest representable ear shade (7 simple colours plus the
    measured 29-shade palette). Palette shades apply to both ears at once --
    a protocol limitation, noted in the entity attributes.
    """

    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_color_mode = ColorMode.HS

    def __init__(self, store: EarPairState, entry: ConfigEntry, side: str) -> None:
        self._store = store
        self._side = side
        base = entry.data["name"]
        self._attr_name = f"{base} {_SIDE_NAMES[side]} Ear"
        self._attr_unique_id = f"{entry.entry_id}-{side}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=base,
            manufacturer="Disney (Made With Magic)",
            model="MWM/GWTS infrared ear pair",
        )
        self._is_on = False
        self._hs_color = None

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def hs_color(self):
        return self._hs_color

    @property
    def effect_list(self) -> list[str] | None:
        return list(LIGHT_EFFECTS)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "side": self._side,
            "running_effect": self._store.running_effect,
            "palette_index": self._store.palette_index,
            "note": (
                "Palette shades apply to both ears at once; simple colors "
                "are set per-ear via fused frames."
            ),
        }

    async def async_turn_on(self, **kwargs) -> None:
        effect = kwargs.get(ATTR_EFFECT)
        hs_color = kwargs.get(ATTR_HS_COLOR)

        if effect is not None:
            index = LIGHT_EFFECTS[effect]
            await self._store.async_invoke_effect(index, effect)
            self._is_on = True
            self.async_write_ha_state()
            return

        if hs_color is not None:
            kind, code = nearest_entry(color_hs_to_RGB(*hs_color))
            if kind == "simple":
                await self._store.async_set_simple(self._side, code)
            else:
                await self._store.async_set_palette(code)
            self._is_on = True
            self._hs_color = hs_color
            self.async_write_ha_state()
            return

        # Bare turn-on with no remembered state: nothing meaningful to send.
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._store.async_set_simple(self._side, 0x60)
        self._is_on = False
        self.async_write_ha_state()
