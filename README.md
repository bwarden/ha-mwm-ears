# Home Assistant integration: `ir_remote_tools`

Controls Disney "Made With Magic" / Glow With The Show ears through the
MQTT infrared rig (Tasmota IR blasters/receivers), exposing them as Home
Assistant entities.

The component is **self-contained**: the MWM protocol library is vendored
under `custom_components/ir_remote_tools/_mwm/` (framing, CRC-8/Dallas,
palette tables, phrase decoder), so no pip install is needed.

## Install

Unpack the distribution into your HA config directory:

```
unzip dist/ir_remote_tools.zip -d ~/.homeassistant/
```

(or copy `custom_components/ir_remote_tools/` by hand). Requires the MQTT
integration to be configured. Restart Home Assistant, then add endpoints
via Settings -> Devices & Services -> Add Integration -> "IR Remote Tools".

## Entities

Each config entry registers one endpoint:

- **Transmitter** (topic you publish Tasmota `IRsend` payloads to):
  two light entities -- *Left Ear* and *Right Ear*. Any MWM ears in range
  of that transmitter follow them together.
  - Pick any colour in HA's picker; it snaps to the nearest of the 7 simple
    ear colours or the measured 29-shade palette. Simple colours are set
    per-ear via fused one-bit phrases; palette shades have no verified
    per-ear phrase and apply to both ears at once.
  - Effects from the verified catalog (`48 XX` invocations): fade out/up,
    pulses, strobe, transitions, rotations, random, blackout.
- **Receiver** (Tasmota topic carrying decoded `IrReceived` results,
  SetOption58 on): three diagnostic sensors --
  - *last message*: raw frame hex plus our decoded interpretation,
  - *assumed state*: best-effort inference of what nearby ears are doing
    (per-ear colours + running effect),
  - *message count*: traffic volume since start.

State inference runs entirely locally: frames are validated (length rule +
CRC-8/Dallas, additive checksum for `55 AA` system messages) and decoded
against the instruction tables in `docs/mwm-show-protocol.md`.

## Development

Tests use the stdlib only (`unittest`, no pytest dependency). The vendored
library is exercised directly; the HA platform modules are syntax-checked
(`compileall`) since homeassistant is not installed here.

```
make test-python   # from repo root, or:
cd python && PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Layout:

| Path | Purpose |
|------|---------|
| `mwm/…` → `_mwm/` | protocol library (vendored into the component) |
| `tests/` | unittest suite for the library |
| `custom_components/ir_remote_tools/` | the integration itself |

`_bootstrap.py` in tests loads `_mwm` as a standalone package so the
library stays testable without homeassistant installed.
