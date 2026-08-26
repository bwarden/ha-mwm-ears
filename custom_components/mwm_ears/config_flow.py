"""Config flow: one entry binds an emitter and/or receiver for MWM ears.

When the user opens the Add dialog, the flow briefly listens (up to 5 s)
for IR signals on all untaken receivers.  The first receiver to hear a
beacon is paired with the emitter that shares its HA device, and that
pair is pre-selected in the form.
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from homeassistant.components import infrared
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.entity_registry import async_get

from .const import CONF_EMITTER_ENTITY, CONF_RECEIVER_ENTITY, DOMAIN

_LOGGER = logging.getLogger(__name__)

_NONE = ""
_NONE_LABEL = "-- not used --"
_DISCOVERY_TIMEOUT_S = 5


def _choices(hass, entity_ids: list[str]) -> dict[str, str]:
    """Map entity ids to friendly labels using the entity registry."""
    registry = async_get(hass)
    choices = {_NONE: _NONE_LABEL}
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        name = entry.name or entry.original_name if entry else None
        choices[entity_id] = f"{name} ({entity_id})" if name else entity_id
    return choices


def _taken_entities(hass) -> set[str]:
    """Collect all infrared entity ids already bound to mwm_ears entries."""
    taken: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        taken.add(entry.data.get(CONF_EMITTER_ENTITY, ""))
        taken.add(entry.data.get(CONF_RECEIVER_ENTITY, ""))
    taken.discard("")
    return taken


def _find_emitter_for_receiver(
    hass, receiver_entity_id: str, untaken_emitters: list[str],
) -> str | None:
    """Find the untaken emitter sharing the same HA device as *receiver_entity_id*."""
    registry = async_get(hass)
    rx_reg = registry.async_get(receiver_entity_id)
    if rx_reg is None or not rx_reg.device_id:
        return None
    for em_id in untaken_emitters:
        em_reg = registry.async_get(em_id)
        if em_reg and em_reg.device_id == rx_reg.device_id:
            return em_id
    return None


async def _scan_for_beacon(
    hass, receivers: list[str], timeout: float = _DISCOVERY_TIMEOUT_S,
) -> str | None:
    """Temporarily listen for IR signals on *receivers*; return first sender.

    Subscribes to every receiver for up to *timeout* seconds.  The first
    receiver to report a signal wins.  All subscriptions are torn down
    before returning, regardless of outcome.
    """
    event = asyncio.Event()
    winner: list[str] = []

    def _make_cb(rid: str):
        @callback
        def cb(_signal) -> None:
            if not winner:
                winner.append(rid)
                event.set()
        return cb

    unsubs: list = []
    for rx_id in receivers:
        try:
            unsub = infrared.async_subscribe_receiver(
                hass, rx_id, _make_cb(rx_id),
            )
            unsubs.append(unsub)
        except Exception:  # noqa: BLE001 – receiver may not be ready yet
            _LOGGER.debug("discovery: could not subscribe to %s", rx_id)

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        for unsub in unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass

    return winner[0] if winner else None


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

        # --- auto-discovery: listen for beacons on untaken receivers ---
        taken = _taken_entities(self.hass)
        untaken_emitters = [e for e in emitters if e not in taken]
        untaken_receivers = [r for r in receivers if r not in taken]

        default_emitter = untaken_emitters[0] if untaken_emitters else None
        default_receiver = untaken_receivers[0] if untaken_receivers else None

        if untaken_receivers:
            discovered_rx = await _scan_for_beacon(self.hass, untaken_receivers)
            if discovered_rx is not None:
                default_receiver = discovered_rx
                em = _find_emitter_for_receiver(
                    self.hass, discovered_rx, untaken_emitters,
                )
                if em is not None:
                    default_emitter = em

        # --- build form with filtered choices ---
        schema: dict = {
            vol.Required("name", default="MWM Ears"): str,
        }
        if untaken_emitters:
            schema[vol.Optional(CONF_EMITTER_ENTITY, default=default_emitter)] = (
                vol.In(_choices(self.hass, untaken_emitters))
            )
        if untaken_receivers:
            schema[vol.Optional(CONF_RECEIVER_ENTITY, default=default_receiver)] = (
                vol.In(_choices(self.hass, untaken_receivers))
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
    """Reconfigure an instance: rename and/or re-select emitter/receiver."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            emitter = user_input.get(CONF_EMITTER_ENTITY) or _NONE
            rx = user_input.get(CONF_RECEIVER_ENTITY) or _NONE
            if not emitter and not rx:
                errors["base"] = "need_one"
            else:
                new_data: dict = {
                    "name": user_input["name"].strip() or "MWM Ears",
                }
                if emitter:
                    new_data[CONF_EMITTER_ENTITY] = emitter
                if rx:
                    new_data[CONF_RECEIVER_ENTITY] = rx

                self.hass.config_entries.async_update_entry(
                    self.entry, data=new_data,
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.entry.entry_id),
                )
                return self.async_create_entry(title="", data={})

        emitters = infrared.async_get_emitters(self.hass)
        receivers = infrared.async_get_receivers(self.hass)
        current_name = self.entry.data.get("name", "")
        current_emitter = self.entry.data.get(CONF_EMITTER_ENTITY, _NONE)
        current_rx = self.entry.data.get(CONF_RECEIVER_ENTITY, _NONE)

        schema: dict = {
            vol.Required("name", default=current_name): str,
        }
        if emitters:
            schema[vol.Optional(CONF_EMITTER_ENTITY, default=current_emitter)] = (
                vol.In(_choices(self.hass, emitters))
            )
        if receivers:
            schema[vol.Optional(CONF_RECEIVER_ENTITY, default=current_rx)] = vol.In(
                _choices(self.hass, receivers)
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
