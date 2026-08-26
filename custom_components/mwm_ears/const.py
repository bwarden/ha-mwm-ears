"""Constants for the IR Remote Tools integration."""

DOMAIN = "mwm_ears"

# Single source of truth for the integration version. manifest.json must
# carry the same value; tests/test_version.py enforces the sync.
INTEGRATION_VERSION = "0.5.0"

CONF_EMITTER_ENTITY = "emitter_entity"
CONF_RECEIVER_ENTITY = "receiver_entity"

# Both entry kinds store one infrared entity id: an emitter for
# transmitters, a receiver for receiver entries.


# hass.data[DOMAIN] key for the shared overheard-traffic hub.
HUB_KEY = "observed_hub"

# Seconds between the spaced re-transmissions of one command. Ear receivers
# drop cold single frames (AGC needs a warm-up edge); genuine wands repeat,
# and ir-mwm-send proved 2x @ ~1.8 s against these very ears.
REPEAT_GAP_S = 1.8

# Seconds of beacon silence after which ears are considered idle.
# MWM peripherals beacon every ~8 s when active and go quiet after
# ~120 s idle, so 130 s gives headroom for reception gaps.
BEACON_TIMEOUT_S = 130
