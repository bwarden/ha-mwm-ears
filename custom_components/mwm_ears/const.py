"""Constants for the IR Remote Tools integration."""

DOMAIN = "mwm_ears"

CONF_EMITTER_ENTITY = "emitter_entity"
CONF_RECEIVER_ENTITY = "receiver_entity"

# Both entry kinds store one infrared entity id: an emitter for
# transmitters, a receiver for receiver entries.


# hass.data[DOMAIN] key for the shared overheard-traffic hub.
HUB_KEY = "observed_hub"
DEVICE_ID = "mwm_ears"  # one shared device across transmitter+receiver entries
# Seconds between the spaced re-transmissions of one command. Ear receivers
# drop cold single frames (AGC needs a warm-up edge); genuine wands repeat,
# and ir-mwm-send proved 2x @ ~1.8 s against these very ears.
REPEAT_GAP_S = 1.8  # one shared device across transmitter+receiver entries
