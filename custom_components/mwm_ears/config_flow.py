"""Config flow: one entry binds an emitter and/or receiver for MWM ears."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import infrared
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.entity_registry import async_get

from .const import CONF_EMITTER_ENTITY, CONF_RECEIVER_ENTITY, DOMAIN

_NONE = ""
_NONE_LABEL = "-- not used --"


def _choices(hass, entity_ids: list[str]) -> dict[str, str]:
    """Map entity ids to friendly labels using the entity registry."""
    registry = async_get(hass)
    choices = {_NONE: _NONE_LABEL}
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        name = entry.name or entry.original_name if entry else None
        choices[entity_id] = f"{name} ({entity_id})" if name else entity_id
    return choices


class MwmEarsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """One integration instance = one MWM room.

    An instance binds an infrared EMITTER (drives the ear lights), an
    infrared RECEIVER (diagnostic sensors), or both. The usual setup is a
    single IR box exposing both, so the form suggests the same device's
    entities for the two fields; either can be left unused.
    """

    VERSION = 2

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        emitters = infrared.async_get_emitters(self.hass)
        receivers = infrared.async_get_receivers(self.hass)
        if not emitters and not receivers:
            return self.async_abort(reason="no_infrared")

        if user_input is not None:
            emitter = user_input.get(CONF_EMITTER_ENTITY) or _NONE
            rx = user_input.get(CONF_RECEIVER_ENTITY) or _NONE
            if not emitter and not rx:
                errors["base"] = "need_one"
            else:
                await self.async_set_unique_id(f"{emitter}|{rx}")
                self._abort_if_unique_id_configured()
                data: dict = {"name": user_input["name"].strip() or "MWM Ears"}
                if emitter:
                    data[CONF_EMITTER_ENTITY] = emitter
                if rx:
                    data[CONF_RECEIVER_ENTITY] = rx
                return self.async_create_entry(title=data["name"], data=data)

        # Same-box setups: preselect this box's emitter + receiver pair.
        schema = {
            vol.Required("name", default="MWM Ears"): str,
        }
        if emitters:
            schema[vol.Optional(CONF_EMITTER_ENTITY, default=emitters[0])] = vol.In(
                _choices(self.hass, emitters)
            )
        if receivers:
            schema[vol.Optional(CONF_RECEIVER_ENTITY, default=receivers[0])] = vol.In(
                _choices(self.hass, receivers)
            )
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(schema), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> MwmEarsOptionsFlow:
        return MwmEarsOptionsFlow(config_entry)


class MwmEarsOptionsFlow(config_entries.OptionsFlow):
    """Rename an instance without re-selecting hardware."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.entry.data.get("name", "")
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("name", default=current): str}),
        )
