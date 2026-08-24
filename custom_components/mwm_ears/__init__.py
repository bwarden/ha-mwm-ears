"""The IR Remote Tools integration.

A consumer of the Home Assistant infrared entity platform (2026.4+).

Each config entry represents ONE MWM room ("Made With Magic") and binds an
infrared **emitter** and/or **receiver** entity -- usually two entities of
the same IR box:

- with an emitter, two light entities -- left ear and right ear -- drive
  every ear in range as a paired set through verified frame forms;
- with a receiver, captured timing signals are decoded into MWM frames,
  feeding diagnostic sensors plus the room-level observed-state hub that
  keeps light entities honest about what wands, hats, and other
  transmitters are doing;
- all instances share one HA device per entry so lights and sensors of an
  IR box appear together.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components import infrared
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from ._mwm import MwmCommand, decode_timings
from .const import (
    CONF_EMITTER_ENTITY,
    CONF_RECEIVER_ENTITY,
    DOMAIN,
    HUB_KEY,
)
from .ears import EarPairState, ObservedHub, ReceiverData

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["light", "sensor"]

def _get_hub(hass: HomeAssistant) -> ObservedHub:
    data = hass.data.setdefault(DOMAIN, {})
    if HUB_KEY not in data:
        data[HUB_KEY] = ObservedHub()
    return data[HUB_KEY]


def _make_signal_handler(receiver: ReceiverData, hub: ObservedHub):
    """Decode received raw timings into frames for sensors + hub."""

    @callback
    def handler(signal) -> None:
        timings = list(getattr(signal, "timings", None) or [])
        if not timings:
            return
        try:
            frames = decode_timings(timings)
        except (ValueError, TypeError):
            _LOGGER.debug("undecodable IR signal on %s", getattr(signal, "modulation", "?"))
            return
        if not frames:
            return  # not MWM (or not decodable); hub counters stay clean
        receiver.ingest(frames)
        hub.ingest(frames)

    return handler


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reject pre-0.3 kind-based entries; they must be removed and re-added."""
    if CONF_EMITTER_ENTITY in entry.data or CONF_RECEIVER_ENTITY in entry.data:
        hass.config_entries.async_update_entry(entry, version=2)
        return True
    _LOGGER.warning(
        "Pre-0.3 %s entries (per-kind transmitter/receiver bindings) are not "
        "migratable to the unified room schema; delete this entry and add it "
        "again.", DOMAIN,
    )
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hub = _get_hub(hass)
    runtime: dict = {"pair": None, "rx": None}
    hass.data[DOMAIN][entry.entry_id] = runtime

    emitter_entity = entry.data.get(CONF_EMITTER_ENTITY)
    if emitter_entity:

        async def transmit(frame: bytes, repeat_count: int) -> None:
            await infrared.async_send_command(
                hass,
                emitter_entity,
                MwmCommand(frame, repeat_count=repeat_count),
            )

        pair = EarPairState(transmit)
        runtime["pair"] = pair
        hub.pairs.append(pair)

        async def refresh(now) -> None:
            await pair.refresh_tick()

        entry.async_on_unload(
            async_track_time_interval(
                hass, refresh, timedelta(seconds=pair.refresh_interval)
            )
        )

        def _detach() -> None:
            if pair in hub.pairs:
                hub.pairs.remove(pair)

        entry.async_on_unload(_detach)

    receiver_entity = entry.data.get(CONF_RECEIVER_ENTITY)
    if receiver_entity:
        receiver_data = ReceiverData()
        runtime["rx"] = receiver_data
        entry.async_on_unload(
            infrared.async_subscribe_receiver(
                hass, receiver_entity, _make_signal_handler(receiver_data, hub)
            )
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # async_on_unload hooks already cancelled timers/subscriptions and
        # detached transmitter pairs from the hub.
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
