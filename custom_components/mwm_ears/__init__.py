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

Entity bindings are resilient: setup always succeeds even when the
underlying IR device is offline.  A state-change listener completes the
subscription (or tears it down) as the device transitions between
available and unavailable, so the entry survives device power-cycles
without manual reload.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components import infrared
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from ._mwm import MwmCommand, decode_timings
from .const import (
    CONF_EMITTER_ENTITY,
    CONF_RECEIVER_ENTITY,
    DOMAIN,
    HUB_KEY,
    REPEAT_GAP_S,
)
from .ears import (
    EarPairState,
    ObservedHub,
    ReceiverData,
    extract_timing_candidates,
)

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
        candidates = extract_timing_candidates(signal)
        receiver.note_signal(signal, candidates)
        frames_out: list[bytes] = []
        for timings in candidates:
            try:
                frames = decode_timings(timings)
            except (ValueError, TypeError):
                continue
            if frames:
                frames_out.extend(frames)
        if not frames_out:
            return  # census kept the evidence; nothing MWM-shaped here
        receiver.ingest(frames_out)
        hub.ingest(frames_out)

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


def _entity_usable(hass: HomeAssistant, entity_id: str | None) -> bool:
    """True when a bound infrared entity exists and has a usable state."""
    if not entity_id:
        return True
    state = hass.states.get(entity_id)
    return state is not None and state.state not in (
        STATE_UNAVAILABLE,
        STATE_UNKNOWN,
    )


def _entity_discovered(hass: HomeAssistant, entity_id: str | None) -> bool:
    """True when the entity has been registered in HA (may be unavailable)."""
    if not entity_id:
        return True
    return hass.states.get(entity_id) is not None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    emitter_entity = entry.data.get(CONF_EMITTER_ENTITY)
    receiver_entity = entry.data.get(CONF_RECEIVER_ENTITY)

    # MQTT-discovered IR entities often appear AFTER our entry is set up at
    # boot.  If the entity has not been discovered at all (state is None),
    # raising NotReady makes HA retry quietly with backoff instead of
    # binding a nonexistent entity id.  If the entity *exists* but is
    # unavailable (device offline), we proceed anyway: a state-change
    # listener will subscribe/unsubscribe as the device recovers.
    missing = [
        e for e in (emitter_entity, receiver_entity)
        if not _entity_discovered(hass, e)
    ]
    if missing:
        raise ConfigEntryNotReady(
            f"infrared entities not yet discovered: {', '.join(missing)}"
        )

    hass.data.setdefault(DOMAIN, {})
    hub = _get_hub(hass)
    runtime: dict = {"pair": None, "rx": None}
    hass.data[DOMAIN][entry.entry_id] = runtime

    if emitter_entity:

        async def transmit(frame: bytes, repeat_count: int) -> None:
            if not _entity_usable(hass, emitter_entity):
                _LOGGER.debug(
                    "emitter %s unavailable, dropping transmit", emitter_entity,
                )
                return
            # repeat_count counts EXTRA spaced transmissions. The framework's
            # own back-to-back repeats don't survive cold ear receivers
            # (rig-verified); the ~1.8 s spacing ir-mwm-send used does.
            for attempt in range(repeat_count + 1):
                if attempt:
                    await asyncio.sleep(REPEAT_GAP_S)
                try:
                    await infrared.async_send_command(
                        hass,
                        emitter_entity,
                        MwmCommand(frame, repeat_count=0),
                    )
                except Exception:  # noqa: BLE001 - caller decides policy
                    _LOGGER.exception(
                        "IR send failed (%s pass %d)", frame.hex(), attempt
                    )
                    raise

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

    if receiver_entity:
        receiver_data = ReceiverData()
        runtime["rx"] = receiver_data
        sub_state: dict = {"unsub": None}

        def _subscribe() -> None:
            sub_state["unsub"] = infrared.async_subscribe_receiver(
                hass, receiver_entity,
                _make_signal_handler(receiver_data, hub),
            )

        def _teardown_sub() -> None:
            unsub = sub_state.get("unsub")
            if callable(unsub):
                try:
                    unsub()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass
            sub_state["unsub"] = None

        # Subscribe now if entity is already available; otherwise wait for
        # the state-change listener to fire when the device comes online.
        if _entity_usable(hass, receiver_entity):
            _subscribe()

        @callback
        def _on_receiver_state(event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                if sub_state.get("unsub") is not None:
                    _teardown_sub()
                    _LOGGER.info(
                        "receiver %s went unavailable, unsubscribed",
                        receiver_entity,
                    )
            else:
                if sub_state.get("unsub") is None:
                    try:
                        _subscribe()
                        _LOGGER.info(
                            "receiver %s available, subscribed",
                            receiver_entity,
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception(
                            "failed to subscribe to %s", receiver_entity,
                        )

        entry.async_on_unload(
            async_track_state_change_event(
                hass, receiver_entity, _on_receiver_state,
            )
        )

        async def _rx_watchdog(now) -> None:
            # The custom infrared integration has been observed to accept
            # a subscription while still initialising and then never
            # dispatch to it -- lights kept working, diagnostic sensors
            # froze until the next restart (rig 2026-08-24). When the
            # framework receiver clearly heard something new but our
            # counters did not move, rebuild the subscription.
            try:
                state = hass.states.get(receiver_entity)
                if state is None or state.state in (
                    STATE_UNAVAILABLE, STATE_UNKNOWN,
                ):
                    return  # entity unavailable; state listener handles it
                prev_ts = sub_state.get("ts")
                prev_seen = sub_state.get("seen")
                sub_state["ts"] = state.last_updated
                sub_state["seen"] = receiver_data.signals_seen
                if prev_ts is None or state.last_updated == prev_ts:
                    return  # nothing new upstream either
                if (
                    prev_seen is not None
                    and receiver_data.signals_seen != prev_seen
                ):
                    return  # healthy: the capture reached our handler
                _teardown_sub()
                try:
                    _subscribe()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception(
                        "watchdog resubscribe failed for %s", receiver_entity,
                    )
                    return
                receiver_data.rebinds += 1
                _LOGGER.warning(
                    "receiver %s went quiet while captures continued; "
                    "resubscribed (rebind %d)",
                    receiver_entity, receiver_data.rebinds,
                )
            except Exception:  # noqa: BLE001 - keep watchdog alive
                _LOGGER.exception(
                    "receiver watchdog error for %s (rebinds=%d)",
                    receiver_entity, receiver_data.rebinds,
                )

        entry.async_on_unload(
            async_track_time_interval(hass, _rx_watchdog, timedelta(seconds=60))
        )

        entry.async_on_unload(_teardown_sub)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # async_on_unload hooks already cancelled timers/subscriptions and
        # detached transmitter pairs from the hub.
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
