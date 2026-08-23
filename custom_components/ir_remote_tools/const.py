"""Constants for the IR Remote Tools integration."""

DOMAIN = "ir_remote_tools"

CONF_KIND = "kind"
KIND_TRANSMITTER = "transmitter"
KIND_RECEIVER = "receiver"

# Both entry kinds store one infrared entity id: an emitter for
# transmitters, a receiver for receiver entries.
CONF_ENTITY_ID = "entity_id"

# hass.data[DOMAIN] key for the shared overheard-traffic hub.
HUB_KEY = "observed_hub"
