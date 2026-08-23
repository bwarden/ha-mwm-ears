"""Config flow: bind an infrared emitter or receiver to an MWM endpoint."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import infrared
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.entity_registry import async_get

from .const import CONF_ENTITY_ID, CONF_KIND, DOMAIN, KIND_RECEIVER, KIND_TRANSMITTER

_KIND_LABELS = {
    KIND_TRANSMITTER: "Transmitter (drives MWM ear lights via an IR emitter)",
    KIND_RECEIVER: "Receiver (diagnostic sensors from captured IR signals)",
}


def _entity_choices(hass, entity_ids: list[str]) -> dict[str, str]:
    """Map entity ids to friendly labels using the entity registry."""
    registry = async_get(hass)
    choices: dict[str, str] = {}
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        name = entry.name or entry.original_name if entry else None
        choices[entity_id] = f"{name} ({entity_id})" if name else entity_id
    return choices


class IrRemoteToolsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an MWM transmitter or receiver binding."""

    VERSION = 1

    def __init__(self) -> None:
        self._kind: str | None = None
        self._name: str | None = None

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._kind = user_input[CONF_KIND]
            self._name = user_input["name"].strip()
            return await self.async_step_hardware()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_KIND, default=KIND_TRANSMITTER): vol.In(
                        _KIND_LABELS
                    ),
                    vol.Required("name"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_hardware(
        self, user_input: dict | None = None
    ) -> FlowResult:
        assert self._kind is not None
        if self._kind == KIND_TRANSMITTER:
            candidates = infrared.async_get_emitters(self.hass)
            abort_reason = "no_emitters"
        else:
            candidates = infrared.async_get_receivers(self.hass)
            abort_reason = "no_receivers"
        if not candidates:
            return self.async_abort(reason=abort_reason)
        if user_input is not None:
            await self.async_set_unique_id(f"{self._kind}:{user_input[CONF_ENTITY_ID]}")
            self._abort_if_unique_id_configured()
            data = {
                CONF_KIND: self._kind,
                "name": self._name,
                CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
            }
            return self.async_create_entry(title=self._name or "", data=data)
        return self.async_show_form(
            step_id="hardware",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENTITY_ID): vol.In(
                        _entity_choices(self.hass, candidates)
                    )
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> IrRemoteToolsOptionsFlow:
        return IrRemoteToolsOptionsFlow(config_entry)


class IrRemoteToolsOptionsFlow(config_entries.OptionsFlow):
    """Rename an endpoint binding without re-selecting hardware."""

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
