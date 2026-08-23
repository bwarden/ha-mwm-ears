"""Config flow for the IR Remote Tools integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_KIND,
    CONF_RECEIVE_TOPIC,
    CONF_TRANSMIT_TOPIC,
    DOMAIN,
    KIND_RECEIVER,
    KIND_TRANSMITTER,
)

_KIND_LABELS = {
    KIND_TRANSMITTER: "IR transmitter (controls MWM ears)",
    KIND_RECEIVER: "IR receiver (diagnostic sensors)",
}


def _schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_KIND, default=defaults.get(CONF_KIND, KIND_TRANSMITTER)): vol.In(
                _KIND_LABELS
            ),
            vol.Required("name", default=defaults.get("name", "")): str,
            vol.Optional(CONF_TRANSMIT_TOPIC, description={"suggested_value": defaults.get(CONF_TRANSMIT_TOPIC, "")}): str,
            vol.Optional(CONF_RECEIVE_TOPIC, description={"suggested_value": defaults.get(CONF_RECEIVE_TOPIC, "")}): str,
        }
    )


class IrRemoteToolsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an MWM transmitter or receiver."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            kind = user_input[CONF_KIND]
            topic_key = (
                CONF_TRANSMIT_TOPIC if kind == KIND_TRANSMITTER else CONF_RECEIVE_TOPIC
            )
            topic = (user_input.get(topic_key) or "").strip()
            if not topic:
                errors["base"] = "missing_topic"
            else:
                await self.async_set_unique_id(f"{kind}:{topic}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input["name"], data=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> IrRemoteToolsOptionsFlow:
        return IrRemoteToolsOptionsFlow(config_entry)


class IrRemoteToolsOptionsFlow(config_entries.OptionsFlow):
    """Rename an endpoint without re-entering topics."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=self.entry.data.get("name", "")): str
                }
            ),
        )
