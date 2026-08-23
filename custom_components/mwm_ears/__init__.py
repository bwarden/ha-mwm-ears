"""The IR Remote Tools integration.

A consumer of the Home Assistant infrared entity platform (2026.4+):

- a *transmitter* config entry binds one infrared **emitter** entity and
  exposes two light entities -- left ear and right ear -- that drive every
  MWM ("Made With Magic") ear in range as a paired set;
- a *receiver* entry binds one infrared **receiver** entity, decodes its
  captured timing signals into MWM frames, and feeds diagnostic sensors
  plus the room-level observed-state hub that keeps light entities honest
  about what wands, hats, and other transmitters are doing.
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
    CONF_ENTITY_ID,
    CONF_KIND,
    DOMAIN,
    HUB_KEY,
    KIND_RECEIVER,
    KIND_TRANSMITTER,
)
from .ears import EarPairState, ObservedHub, ReceiverData

_LOGGER = logging.getLogger(__name__)

PLATFORMS_BY_KIND = {
    KIND_TRANSMITTER: ["light"],
    KIND_RECEIVER: ["sensor"],
}

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    kind = entry.data[CONF_KIND]
    hub = _get_hub(hass)
    ir_entity_id = entry.data[CONF_ENTITY_ID]

    if kind == KIND_TRANSMITTER:
        async def transmit(frame: bytes, repeat_count: int) -> None:
            await infrared.async_send_command(
                hass,
                ir_entity_id,
                MwmCommand(frame, repeat_count=repeat_count),
            )

        store = EarPairState(transmit)
        hass.data[DOMAIN][entry.entry_id] = store
        hub.pairs.append(store)

        async def refresh(now) -> None:
            await store.refresh_tick()

        entry.async_on_unload(
            async_track_time_interval(
                hass, refresh, timedelta(seconds=store.refresh_interval)
            )
        )

        def _detach() -> None:
            if store in hub.pairs:
                hub.pairs.remove(store)

        entry.async_on_unload(_detach)
    else:
        receiver_data = ReceiverData()
        hass.data[DOMAIN][entry.entry_id] = receiver_data
        entry.async_on_unload(
            infrared.async_subscribe_receiver(
                hass, ir_entity_id, _make_signal_handler(receiver_data, hub)
            )
        )

    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS_BY_KIND[kind]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_BY_KIND[entry.data[CONF_KIND]]
    )
    if unload_ok:
        # async_on_unload hooks already cancelled timers/subscriptions and
        # detached transmitter pairs from the hub.
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
