"""Light platform: left/right MWM ear lights bound to an infrared emitter."""

from __future__ import annotations

import logging

from homeassistant.components.light import (
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util.color import color_hs_to_RGB, color_RGB_to_hs

from ._mwm import EAR_STATE_OFF, SIMPLE_COLORS, nearest_entry
from .const import CONF_EMITTER_ENTITY, DEVICE_ID, DOMAIN, HUB_KEY
from .ears import (
    BOTH,
    EAR_OFF_CODE,
    LEFT,
    RIGHT,
    EarPairState,
    ObservedHub,
)

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

_SIDE_NAMES = {BOTH: "Both", LEFT: "Left", RIGHT: "Right"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    store: EarPairState = runtime["pair"]  # set: emitter half configured
    hub: ObservedHub = hass.data[DOMAIN][HUB_KEY]
    name = entry.data["name"]
    if not entry.data.get(CONF_EMITTER_ENTITY):
        return  # this instance has no emitter half
    async_add_entities(
        [
            MwmEarLight(store, hub, entry, BOTH),
            MwmEarLight(store, hub, entry, LEFT),
            MwmEarLight(store, hub, entry, RIGHT),
        ]
    )


class MwmEarLight(LightEntity):
    """One ear of the pair driven through an infrared emitter.

    Both entities control the same physical room of ears; composing the
    both-ears and right-only primitives keeps their simple colors
    independent. Any HS colour picked in HA
    snaps to the nearest representable ear shade (7 simple colours plus the
    measured 29-shade palette). Palette shades apply to both ears at once --
    a protocol limitation, noted in the entity attributes. When a foreign
    MWM command is overheard, the entity shows the suspension instead of
    pretending our last command still rules the room.
    """

    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_color_mode = ColorMode.HS
    # Without this flag HA never renders the effects drop-down even
    # though effect_list is populated.
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(
        self,
        store: EarPairState,
        hub: ObservedHub,
        entry: ConfigEntry,
        side: str,
    ) -> None:
        self._store = store
        self._hub = hub
        self._side = side
        base = entry.data["name"]
        suffix = (
            f"{_SIDE_NAMES[side]} Ear" if side != BOTH else "Both Ears"
        )
        self._attr_name = f"{base} {suffix}"
        self._attr_unique_id = f"{entry.entry_id}-{side}"
        # Transmitter and receiver entries deliberately share ONE device:
        # the IR hardware is usually a single box, and even split rx/tx acts
        # as one logical MWM room controller.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, DEVICE_ID)},
            name="MWM Ears",
            manufacturer="Disney (Made With Magic)",
            model="MWM/GWTS infrared room controller",
        )
        self._is_on = False

    async def async_added_to_hass(self) -> None:
        self._store.listeners.append(self.async_write_ha_state)
        self._hub.listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        for listeners in (self._store.listeners, self._hub.listeners):
            try:
                listeners.remove(self.async_write_ha_state)
            except ValueError:
                pass

    @property
    def is_on(self) -> bool:
        if self._side == BOTH:
            return self._is_on and all(
                self._store.side_color_name(s) != EAR_STATE_OFF
                for s in (LEFT, RIGHT)
            )
        return (
            self._is_on
            and self._store.side_color_name(self._side) != EAR_STATE_OFF
        )

    @property
    def effect_list(self) -> list[str] | None:
        return list(LIGHT_EFFECTS)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "side": self._side,
            "running_effect": self._store.running_effect,
            "palette_code": dict(self._store.palette_code),
            "suspended_by": self._store.suspended_by,
            "room_state": self._hub.snapshot(),
            "note": (
                "Right-ear picks use verified right-only frames directly; "
                "left picks coordinate both-ears + right-only frames "
                "(left palette picks fall back to both ears unless the "
                "right ear already holds a palette shade). Repeats pause "
                "while a foreign MWM command is in charge."
            ),
        }

    async def async_turn_on(self, **kwargs) -> None:
        effect = kwargs.get(ATTR_EFFECT)
        hs_color = kwargs.get(ATTR_HS_COLOR)

        # Optimistic state FIRST: the transmit chain spans seconds (spaced
        # repeats); setting _is_on afterwards let one flaky emitter call
        # slide a lit switch back to off even though the IR had landed.
        self._is_on = True

        if effect is not None:
            index = LIGHT_EFFECTS[effect]
            await self._store.apply_effect(index, effect)
            return

        if hs_color is not None:
            kind, code = nearest_entry(color_hs_to_RGB(*hs_color))
            if kind == "simple":
                if self._side == BOTH:
                    await self._store.apply_simple_both(code)
                else:
                    await self._store.apply_simple(self._side, code)
            elif self._side == BOTH:
                await self._store.apply_palette(code)
            else:
                await self._store.apply_palette(code, side=self._side)
            self._attr_hs_color = hs_color
            return

        # Bare turn-on: restore the remembered color instead of faking an
        # on state that sends nothing IR.
        if self._side == BOTH:
            code = await self._store.turn_on_both()
        else:
            code = await self._store.turn_on_side(self._side)
        rgb = SIMPLE_COLORS[code][1]
        self._attr_hs_color = color_RGB_to_hs(*rgb)

    async def async_turn_off(self, **kwargs) -> None:
        if self._side == BOTH:
            await self._store.apply_simple_both(EAR_OFF_CODE)
        else:
            await self._store.turn_off_side(self._side)
        self._is_on = False
