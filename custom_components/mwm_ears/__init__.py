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
from pathlib import Path

from homeassistant.components import infrared
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import (
    CONF_RESOURCE_TYPE_WS,
    DOMAIN as LL_DOMAIN,
)
from homeassistant.components.lovelace.resources import (
    ResourceStorageCollection,
    ResourceYAMLCollection,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_URL, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)

from ._mwm import MwmCommand, decode_timings
from .const import (
    BEACON_TIMEOUT_S,
    CONF_EMITTER_ENTITY,
    CONF_RECEIVER_ENTITY,
    DOMAIN,
    HUB_KEY,
    INTEGRATION_VERSION,
    REPEAT_GAP_S,
)
from .ears import (
    ENFORCE_INTERVAL_S,
    EarPairState,
    ObservedHub,
    ReceiverData,
    extract_timing_candidates,
)
from .show import ShowPlayer

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["light", "sensor", "switch"]


def _get_hub(hass: HomeAssistant) -> ObservedHub:
    data = hass.data.setdefault(DOMAIN, {})
    if HUB_KEY not in data:
        data[HUB_KEY] = ObservedHub()
    return data[HUB_KEY]


def _make_signal_handler(
    hass: HomeAssistant,
    receiver: ReceiverData,
    hub: ObservedHub,
    receiver_entity: str | None = None,
):
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
        # Enforcing pairs that heard a foreign (non-echo) signal are pulled
        # back onto their held state immediately (enforce_reassert is a
        # no-op for non-enforcing pairs, so scheduling it is always safe).
        for pair in hub.ingest(frames_out, receiver=receiver_entity):
            hass.async_create_task(pair.enforce_reassert())

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
    """True when a bound infrared entity exists and is not explicitly offline.

    Only ``unavailable`` blocks: IR transmitters in practice rest at
    ``unknown`` (an MQTT/Tasmota transmitter never publishes a state), so
    treating ``unknown`` as unusable would silently drop every transmission
    for what is a perfectly healthy device. ``None`` (not yet discovered)
    stays unusable -- setup gates on discovery and the receiver
    subscribe/watchdog paths handle the rest.
    """
    if not entity_id:
        return True
    state = hass.states.get(entity_id)
    return state is not None and state.state != STATE_UNAVAILABLE


def _entity_discovered(hass: HomeAssistant, entity_id: str | None) -> bool:
    """True when the entity has been registered in HA (may be unavailable)."""
    if not entity_id:
        return True
    return hass.states.get(entity_id) is not None


_CARD_FRONTEND_URL = "/custom_components/mwm_ears/frontend"
# Module URL with the integration version as a cache buster. The Lovelace
# module loader and browsers key on the URL, so an unversioned entry keeps
# serving a stale bundled card after an update; bumping the version changes
# the URL and forces a fresh fetch (_sync_card_resource rewrites the
# registered resource whenever the version changes).
_CARD_MODULE_URL = f"{_CARD_FRONTEND_URL}/mwm-ears-card.js?v={INTEGRATION_VERSION}"
_cards_registered = False

_CARD_RESOURCE_LOCK = "card_resource_lock"
_CARD_RESOURCE_DONE = "card_resource_done"
_CARD_RESOURCE_BY_US = "card_resource_by_us"


async def _serve_frontend(hass: HomeAssistant) -> None:
    """Serve the bundled Lovelace card straight from this package, once.

    Unpacking the integration should be enough to use the card: no manual
    copy into HA's www/ directory is required. Registration is idempotent,
    and setup must never fail because serving the card failed -- the README
    documents the www/ fallback for installs where static registration is
    unavailable.

    cache_headers=False keeps the file out of the long-lived HTTP cache so
    the card's versioned module URL decides freshness: a replaced file is
    re-validated on the next fetch instead of pinning the previous card.
    """
    global _cards_registered
    if _cards_registered:
        return
    try:
        frontend_dir = Path(__file__).resolve().parent / "frontend"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    _CARD_FRONTEND_URL, str(frontend_dir), cache_headers=False
                )
            ]
        )
    except Exception:
        _LOGGER.warning(
            "Unable to serve the MWM Ears Lovelace card at %s; copy "
            "frontend/mwm-ears-card.js into your HA www/ directory instead"
            " (see python/README.md).", _CARD_FRONTEND_URL,
        )
    else:
        _cards_registered = True


async def _sync_card_resource(hass: HomeAssistant) -> None:
    """Register the card module as a Lovelace resource, replacing stale ones.

    Storage-mode dashboards (the default) get the versioned _CARD_MODULE_URL
    as a ``module`` resource automatically -- no manual Resources step and no
    HACS-style helper. Any earlier entry pointing at the same unversioned or
    older mwm-ears-card.js path is replaced, which is what evicts a stale
    card after an update. YAML-mode resources cannot be edited from here, so
    that mode is pointed at the URL to add by hand instead.
    """
    resources = getattr(hass.data.get(LL_DOMAIN), "resources", None)
    if resources is None:
        _LOGGER.debug(
            "Lovelace not loaded yet; card resource registration deferred "
            "to the next setup"
        )
        return
    if isinstance(resources, ResourceYAMLCollection):
        if not any(
            item.get(CONF_URL) == _CARD_MODULE_URL
            for item in resources.async_items()
        ):
            _LOGGER.warning(
                "Card resources are in YAML mode, which the integration "
                "cannot edit; add this entry to your lovelace resources:\n"
                '  - url: "%s"\n    type: module',
                _CARD_MODULE_URL,
            )
        return
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    items = resources.async_items()
    if any(item.get(CONF_URL) == _CARD_MODULE_URL for item in items):
        return
    for item in items:
        if item.get(CONF_URL, "").startswith(
            f"{_CARD_FRONTEND_URL}/mwm-ears-card.js"
        ):
            await resources.async_delete_item(item[CONF_ID])
    created = await resources.async_create_item(
        {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: _CARD_MODULE_URL}
    )
    hass.data[DOMAIN][_CARD_RESOURCE_BY_US] = True
    _LOGGER.info(
        "Registered the MWM Ears card for dashboards (%s)", created[CONF_URL],
    )


async def _ensure_card_resource(hass: HomeAssistant) -> None:
    """Run _sync_card_resource once per process without failing setup."""
    if hass.data[DOMAIN].get(_CARD_RESOURCE_DONE):
        return
    async with hass.data[DOMAIN].setdefault(_CARD_RESOURCE_LOCK, asyncio.Lock()):
        if hass.data[DOMAIN].get(_CARD_RESOURCE_DONE):
            return
        try:
            await _sync_card_resource(hass)
        except Exception:  # noqa: BLE001 - setup must still succeed
            _LOGGER.exception("Failed to register the MWM Ears card resource")
        finally:
            hass.data[DOMAIN][_CARD_RESOURCE_DONE] = True


async def _cleanup_card_resource(hass: HomeAssistant) -> None:
    """Drop the card resource we registered once the last entry unloads."""
    try:
        if not hass.data[DOMAIN].get(_CARD_RESOURCE_BY_US):
            return
        resources = getattr(hass.data.get(LL_DOMAIN), "resources", None)
        if isinstance(resources, ResourceStorageCollection):
            if not resources.loaded:
                await resources.async_load()
                resources.loaded = True
            for item in resources.async_items():
                if item.get(CONF_URL) == _CARD_MODULE_URL:
                    await resources.async_delete_item(item[CONF_ID])
        # Un-done, so a later setup re-registers the module.
        hass.data[DOMAIN].pop(_CARD_RESOURCE_DONE, None)
        hass.data[DOMAIN].pop(_CARD_RESOURCE_BY_US, None)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed to remove the MWM Ears card resource")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    emitter_entity = entry.data.get(CONF_EMITTER_ENTITY)
    receiver_entity = entry.data.get(CONF_RECEIVER_ENTITY)

    # Shared runtime dict must exist before _ensure_card_resource writes into
    # it (e.g. the _CARD_RESOURCE_BY_US marker).
    hass.data.setdefault(DOMAIN, {})

    await _serve_frontend(hass)
    await _ensure_card_resource(hass)

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

    hub = _get_hub(hass)
    runtime: dict = {"pair": None, "rx": None}
    hass.data[DOMAIN][entry.entry_id] = runtime

    if emitter_entity:

        emitter_drop_logged = False

        async def transmit(frame: bytes, repeat_count: int) -> None:
            nonlocal emitter_drop_logged
            if not _entity_usable(hass, emitter_entity):
                # Warn once per lapse, not per frame -- a multi-frame show
                # would otherwise flood the log.
                if not emitter_drop_logged:
                    state = hass.states.get(emitter_entity)
                    _LOGGER.warning(
                        "emitter %s unavailable (%s), dropping IR transmissions",
                        emitter_entity,
                        state.state if state else "not discovered",
                    )
                    emitter_drop_logged = True
                return
            emitter_drop_logged = False
            _LOGGER.debug(
                "MWM send to %s via %s (repeat_count=%d)",
                pair.room_name, emitter_entity, repeat_count,
            )
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
        pair.receiver_entity = receiver_entity
        pair.emitter_entity = emitter_entity
        pair.room_name = entry.data["name"]
        runtime["pair"] = pair
        hub.pairs.append(pair)

        # .msh show replay: each frame goes out through the same mark-sent
        # path as a user command, so our own show echoes are recognised and
        # the partner sensor/watchdogs stay consistent.  The player lives on
        # the pair so the play_show/stop_show services (registered by the
        # light platform) can reach it, and is cancelled on unload.
        player = ShowPlayer(pair.replay_frame)
        pair.show_player = player
        runtime["show"] = player
        entry.async_on_unload(player.stop)

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
                _make_signal_handler(hass, receiver_data, hub, receiver_entity),
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

    # Beacon-silence watchdog: shared across all entries (runs once).
    hub = _get_hub(hass)
    _BEACON_WDT_KEY = "_beacon_watchdog"
    if _BEACON_WDT_KEY not in hass.data[DOMAIN]:

        async def _beacon_watchdog(_now) -> None:
            if hub.check_beacon_timeout(BEACON_TIMEOUT_S):
                hub._notify()

        hass.data[DOMAIN][_BEACON_WDT_KEY] = True
        entry.async_on_unload(
            async_track_time_interval(
                hass, _beacon_watchdog, timedelta(seconds=30),
            )
        )

    # Enforcement heartbeat: shared across all entries (runs once).  Each
    # tick re-asserts the held state for every pair with enforcement ON.
    # Non-enforcing pairs are no-ops (see EarPairState.enforce_tick).
    _ENFORCE_KEY = f"{DOMAIN}_enforce_ticker"
    if _ENFORCE_KEY not in hass.data[DOMAIN]:

        async def _enforce_ticker(_now) -> None:
            for pair in hub.pairs:
                await pair.enforce_tick()

        hass.data[DOMAIN][_ENFORCE_KEY] = True
        entry.async_on_unload(
            async_track_time_interval(
                hass, _enforce_ticker, timedelta(seconds=ENFORCE_INTERVAL_S),
            )
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply "mwm" label to all mwm_ears entities.
    # Use a one-shot timer to defer past concurrent entry setup.
    _LABEL_DONE = f"{DOMAIN}_labels_applied"
    if _LABEL_DONE not in hass.data[DOMAIN]:
        hass.data[DOMAIN][_LABEL_DONE] = True

        @callback
        def _apply_labels(_now) -> None:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(hass)
            for eid, reg_entry in registry.entities.items():
                if reg_entry.platform == DOMAIN:
                    registry.async_update(eid, labels={"mwm"})

        async_call_later(hass, 5, _apply_labels)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # async_on_unload hooks already cancelled timers/subscriptions and
        # detached transmitter pairs from the hub.
        hass.data[DOMAIN].pop(entry.entry_id, None)
    if not any(
        other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(
            DOMAIN, include_disabled=False, include_ignore=False
        )
    ):
        await _cleanup_card_resource(hass)
    return unload_ok
