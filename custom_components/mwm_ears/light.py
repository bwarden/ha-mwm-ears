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
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util.color import color_hs_to_RGB

from ._mwm import (
    LIGHT_EFFECTS,
    PALETTE,
    SIMPLE_COLORS,
    color_palette,
    nearest_entry,
    parse_color,
)
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

_SIDE_NAMES = {BOTH: "Both", LEFT: "Left", RIGHT: "Right"}

# Live light instances by entity id, for the domain's exact-color services
# (exact-command picks that bypass color-wheel snapping).
_BY_ENTITY_ID: dict[str, "MwmEarLight"] = {}
_services_registered = False


async def _select_color_service(call: ServiceCall) -> None:
    """Handle mwm_ears.select_color: send one exact catalog color."""
    target = call.data.get(ATTR_ENTITY_ID)
    entity_id = target[0] if isinstance(target, (list, tuple)) else target
    light = _BY_ENTITY_ID.get(entity_id)
    if light is None:
        raise ServiceValidationError(f"not an MWM Ears light entity: {entity_id}")
    color = call.data.get("color")
    if not isinstance(color, str):
        raise ServiceValidationError("'color' must be a kind:value string")
    await light.async_select_color(color)


async def _set_state_service(call: ServiceCall) -> None:
    """Handle mwm_ears.set_state: native pair-level control for automations.

    Richer than light.turn_on: one action sets both ears' exact colors --
    independently or as a native both pick, each "off"-able -- and runs a
    room-wide effect. Target any of the pair's lights or the mwm_ears device
    (target expansion resolves to the shared EarPairState). Lays the pair
    ground for later timing/sync/countdown fields without further entry
    plumbing.
    """
    target = call.data.get(ATTR_ENTITY_ID)
    ids = target if isinstance(target, (list, tuple)) else [target]
    light = None
    for entity_id in ids:
        if entity_id in _BY_ENTITY_ID:
            light = _BY_ENTITY_ID[entity_id]
            break
    if light is None:
        raise ServiceValidationError("no MWM Ears light entity targeted")
    store: EarPairState = light._store

    left = _parse_state_pick(call.data.get("left_color"))
    right = _parse_state_pick(call.data.get("right_color"))
    both = _parse_state_pick(call.data.get("color"))

    incantation = call.data.get("incantation")
    if incantation is not None:
        family = str(incantation).strip().lower()
        if left is None:
            left = store._ear_color(LEFT)
        if right is None:
            right = store._ear_color(RIGHT)
        label = f"show {family}"
        try:
            await store.apply_incantation(
                family, label=label, left=left, right=right
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        return

    effect = None
    effect_spec = call.data.get("effect")
    if effect_spec:
        index = LIGHT_EFFECTS.get(effect_spec)
        if index is None:
            raise ServiceValidationError(f"unknown effect: {effect_spec!r}")
        effect = (index, effect_spec)

    await store.apply_state(left=left, right=right, both=both, effect=effect)


def _parse_state_pick(spec) -> tuple[str, int] | None:
    """Resolve a set_state color field to a parsed pick.

    Accepts the same values as ``parse_color`` plus ``"off"``; None (field
    unset) means "don't touch this ear". Raises ServiceValidationError for
    anything malformed.
    """
    if spec is None:
        return None
    if not isinstance(spec, str):
        raise ServiceValidationError("'color' fields must be a name, selector, or \"off\"")
    text = spec.strip()
    if text.lower() == "off":
        return ("simple", EAR_OFF_CODE)
    try:
        return parse_color(text)
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err


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

    global _services_registered
    if not _services_registered:
        hass.services.async_register(DOMAIN, "select_color", _select_color_service)
        hass.services.async_register(DOMAIN, "set_state", _set_state_service)
        _services_registered = True

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
    both-ears and left-only primitives keeps their simple colors
    independent. Any HS color picked in HA
    snaps to the nearest representable ear shade (7 simple colors plus the
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
            "Ears" if side == BOTH else f"{_SIDE_NAMES[side]} Ear"
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

    async def async_added_to_hass(self) -> None:
        self._store.listeners.append(self.async_write_ha_state)
        self._hub.listeners.append(self.async_write_ha_state)
        _BY_ENTITY_ID[self.entity_id] = self

    async def async_will_remove_from_hass(self) -> None:
        _BY_ENTITY_ID.pop(self.entity_id, None)
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
                or self._store.desired_on[RIGHT]
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
    def color_identity(self) -> dict | None:
        """Exact catalog identity currently on this ear, for the card.

        ``{"kind": "simple", "code": 0x61}`` or ``{"kind": "palette",
        "index": 4}`` -- the shape the card's swatches match against.  Exact
        ``select_color`` picks and color-wheel picks both land here because
        they write the same codes/palette indices; near-identical shades that
        are distinct commands (simple 0x61 blue vs palette 0x04 pure blue)
        stay distinguishable, which RGB through the HS round-trip cannot.
        """
        return self._store.side_picked(self._side)

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
            "color_identity": self.color_identity,
            "palette_code": dict(self._store.palette_code),
            "suspended_by": self._store.suspended_by,
            "room_state": self._hub.snapshot(),
            # Catalog every representable ear color (name, RGB, kind, and
            # the protocol code/index), for the Lovelace card and any other
            # consumer. Identical on all three side entities -- each side can
            # express every color; only the transmitted frame differs (held
            # in light.py / ears.py, not here).
            "color_palette": color_palette(),
            "note": (
                "Left-ear picks use verified left-only frames directly; "
                "right picks fuse the pair into one frame so the left ear "
                "keeps its color (palette or simple) with no flash or "
                "clobber. Repeats pause while a foreign MWM command is in "
                "charge."
            ),
        }

    async def async_select_color(self, color_spec: str) -> None:
        """Send one EXACT catalog color, bypassing color-wheel snapping.

        ``color_spec`` is a catalog name ("lime green", "pure blue") or a
        "kind:value" selector ("simple:0x61", "palette:4"); ``parse_color``
        validates it.  This is the only way to send near-identical shades
        (simple 0x61 blue vs palette 0x04 pure blue) as distinct commands.
        """
        kind, code = parse_color(color_spec)
        if kind == "simple":
            if self._side == BOTH:
                await self._store.apply_simple_both(code)
            else:
                await self._store.apply_simple(self._side, code)
        elif self._side == BOTH:
            await self._store.apply_palette(code)
        else:
            await self._store.apply_palette(code, side=self._side)

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


