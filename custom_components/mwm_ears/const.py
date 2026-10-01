"""Constants for the MWM Ears integration."""

DOMAIN = "mwm_ears"

# Single source of truth for the integration version. manifest.json must
# carry the same value; tests/test_contract.py enforces the sync.
INTEGRATION_VERSION = "0.8.5"

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

# Tuya-firmware S18 IR blasters compress the MWM grid: every space is
# shortened by a fixed ~88 us (mark+space period per bit falls short of the
# nominal 2 x 417 us, pushing the effective baud to ~2600 vs 2400). Tolerant
# receivers still decode it, but the ears' strict tick clock does not. The
# S18 hardware is fine under ESPHome/LibreTiny (correct 2402 baud), so this
# is firmware-specific and keyed off the emitter's identity, not its room.
# Pre-stretching every space by this offset restores the nominal grid.
# (measured 2026-09; see README "Known issue: Tuya-firmware S18").
TUYA_S18_SPACE_COMP_US = 88

# Emitter identity (firmware) that requires space compensation: the entity is
# exposed by the tuya_local integration AND its owning config entry selects
# the basic_ir_remote device driver (the S18 family). Reflected hardware
# (ESPHome/LibreTiny on the same board) is exposed by esphome, and other
# emitters (Tasmota/mqtt) reproduce the grid exactly -- none need it.
TUYA_S18_PLATFORM = "tuya_local"
TUYA_S18_DEVICE_TYPE = "basic_ir_remote"

# Passive assume-off: seconds of beacon silence after which a light that is
# ON is taken to mean the ears powered off (so the light entities reflect
# off).  Only applies while NOT enforcing; enforcing pairs re-assert their
# state instead and never assume off.  IMPORTANT: ears are SILENT for ~2 min
# after successfully receiving a command, then enter demo mode and begin
# beaconing again (~every 8 s).  A light that is genuinely on therefore goes
# quiet for up to ~120 s before beacons resume, so the assume-off window must
# exceed that or it would wrongly turn the light off mid-quiet-window.  180 s
# = ~3 min quiet is a real power-off / out-of-range signal.
BEACON_TIMEOUT_S = 180
