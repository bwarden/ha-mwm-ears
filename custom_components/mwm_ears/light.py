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
from homeassistant.helpers import entity_registry as er
from homeassistant.util.color import color_hs_to_RGB

from ._mwm import nearest_entry
from .const import CONF_EMITTER_ENTITY, INTEGRATION_VERSION, DOMAIN, HUB_KEY
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

AGGREGATE_DEVICE_ID = f"{DOMAIN}_all_rooms"


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

    # Aggregate light: created once when 2+ emitter entries exist.
    _AGG_KEY = f"{DOMAIN}_aggregate_light"
    if _AGG_KEY not in hass.data[DOMAIN] and len(hub.pairs) >= 2:
        hass.data[DOMAIN][_AGG_KEY] = True
        async_add_entities([MwmAggregateLight(hub)])


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"mwm_ears_{entry.entry_id}")},
            name="MWM Ears",
            manufacturer="Disney (Made With Magic)",
            model="MWM/GWTS infrared room controller",
            sw_version=INTEGRATION_VERSION,
        )
        self._attr_labels = {"mwm"}

    async def async_added_to_hass(self) -> None:
        self._store.listeners.append(self.async_write_ha_state)
        self._hub.listeners.append(self.async_write_ha_state)
        if self.entity_id:
            registry = er.async_get(self.hass)
            registry.async_update(self.entity_id, labels={"mwm"})

    async def async_will_remove_from_hass(self) -> None:
        for listeners in (self._store.listeners, self._hub.listeners):
            try:
                listeners.remove(self.async_write_ha_state)
            except ValueError:
                pass

    @property
    def is_on(self) -> bool:
        if self._side == BOTH:
            return (
                self._store.desired_on[LEFT]
                and self._store.desired_on[RIGHT]
            )
        return self._store.desired_on[self._side]

    @property
    def hs_color(self) -> tuple[float, float] | None:
        return self._store.side_hs_color(self._side)

    @property
    def effect(self) -> str | None:
        if self._side == BOTH:
            return self._store.running_effect
        return None

    @property
    def effect_list(self) -> list[str] | None:
        # Effect programs are room-wide: a wand command re-programs every
        # ear in range regardless of which entity issued it. Offering the
        # selector on the per-ear entities would suggest per-ear control
        # that the protocol cannot deliver.
        if self._side != BOTH:
            return None
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
            return

        # Bare turn-on: restore the remembered color instead of faking an
        # on state that sends nothing IR.
        if self._side == BOTH:
            await self._store.turn_on_both()
        else:
            await self._store.turn_on_side(self._side)

    async def async_turn_off(self, **kwargs) -> None:
        if self._side == BOTH:
            await self._store.apply_simple_both(EAR_OFF_CODE)
        else:
            await self._store.turn_off_side(self._side)


class MwmAggregateLight(LightEntity):
    """Top-level light controlling all MWM ear pairs across rooms.

    is_on is True when ANY sub-light is on.  hs_color and effect
    reflect the most recently active room.
    """

    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_color_mode = ColorMode.HS
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_name = "MWM All Rooms"
    _attr_unique_id = f"{DOMAIN}_aggregate_light"
    _attr_labels = {"mwm"}

    def __init__(self, hub: ObservedHub) -> None:
        self._hub = hub
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, AGGREGATE_DEVICE_ID)},
            name="MWM Ears",
            manufacturer="Disney (Made With Magic)",
            model="MWM/GWTS aggregate controller",
            sw_version=INTEGRATION_VERSION,
        )

    async def async_added_to_hass(self) -> None:
        self._hub.listeners.append(self.async_write_ha_state)
        if self.entity_id:
            registry = er.async_get(self.hass)
            registry.async_update(self.entity_id, labels={"mwm"})

    async def async_will_remove_from_hass(self) -> None:
        try:
            self._hub.listeners.remove(self.async_write_ha_state)
        except ValueError:
            pass

    def _active_pairs(self) -> list[EarPairState]:
        """Pairs whose ears are on (at least one side)."""
        return [p for p in self._hub.pairs if any(p.desired_on.values())]

    def _most_recent_pair(self) -> EarPairState | None:
        """The most recently active pair, or None if none active."""
        active = self._active_pairs()
        if not active:
            return None
        return max(active, key=lambda p: p._last_active_at)

    @property
    def is_on(self) -> bool:
        return bool(self._active_pairs())

    @property
    def hs_color(self) -> tuple[float, float] | None:
        pair = self._most_recent_pair()
        if pair is None:
            return None
        # Use BOTH side for aggregate color
        return pair.side_hs_color(BOTH)

    @property
    def effect(self) -> str | None:
        pair = self._most_recent_pair()
        if pair is None:
            return None
        return pair.running_effect

    @property
    def effect_list(self) -> list[str] | None:
        return list(LIGHT_EFFECTS)

    @property
    def extra_state_attributes(self) -> dict:
        rooms = {}
        for pair in self._hub.pairs:
            rooms[pair.room_name or "unknown"] = {
                "on": any(pair.desired_on.values()),
                "left_on": pair.desired_on[LEFT],
                "right_on": pair.desired_on[RIGHT],
                "hs_color": pair.side_hs_color(BOTH),
                "running_effect": pair.running_effect,
                "suspended_by": pair.suspended_by,
            }
        return {
            "rooms": rooms,
            "active_rooms": [
                p.room_name for p in self._active_pairs()
            ],
        }

    async def async_turn_on(self, **kwargs) -> None:
        effect = kwargs.get(ATTR_EFFECT)
        hs_color = kwargs.get(ATTR_HS_COLOR)

        if effect is not None:
            index = LIGHT_EFFECTS[effect]
            for pair in self._hub.pairs:
                await pair.apply_effect(index, effect)
            return

        if hs_color is not None:
            kind, code = nearest_entry(color_hs_to_RGB(*hs_color))
            for pair in self._hub.pairs:
                if kind == "simple":
                    await pair.apply_simple_both(code)
                else:
                    await pair.apply_palette(code)
            return

        for pair in self._hub.pairs:
            await pair.turn_on_both()

    async def async_turn_off(self, **kwargs) -> None:
        for pair in self._hub.pairs:
            await pair.apply_simple_both(EAR_OFF_CODE)
