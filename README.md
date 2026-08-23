# Home Assistant integration: `ir_remote_tools`

Controls Disney "Made With Magic" / Glow With The Show ears through Home
Assistant's **infrared entity platform** (HA 2026.4+). Requires at least
one infrared proxy configured in HA (e.g. an ESPHome IR/RF proxy); the
integration is a *consumer* on that platform, not a hardware transport
itself.

The component is self-contained: the MWM protocol library is vendored
under `custom_components/ir_remote_tools/_mwm/` (framing, CRC-8/Dallas,
palette tables, phrase decoder, timing codec). The only external
requirement is `infrared-protocols`, pulled in via the manifest for the
framework's `Command` envelope type; MWM support itself lives here.

## Install

Unpack the distribution into your HA config directory:

```
unzip dist/ir_remote_tools.zip -d ~/.homeassistant/
```

(or copy `custom_components/ir_remote_tools/` by hand). Restart Home
Assistant, then add endpoints via Settings -> Devices & Services -> Add
Integration -> "IR Remote Tools". Each endpoint binds one infrared entity:

- **Transmitter** = infrared *emitter* entity. Exposes two lights --
  *Left Ear* and *Right Ear* -- driving every MWM ear in range together.
  - Any colour from HA's picker snaps to the nearest representable shade:
    the 7 simple colours go per-ear as fused one-bit phrases (`91 cL cR`);
    palette shades use the rig-verified both-ears template (no verified
    per-ear form exists).
  - Effect list invokes verified `48 XX` programs (fades, pulses, strobe,
    transitions, rotations, random, blackout).
  - While both ears are on, the current colour phrase is re-issued every
    ~8 s so newly-powered ears join in. **Off is never repeated** beyond
    its initial multi-burst, so ears under independent control are left
    alone; effect invocations likewise fire once (restarting would glitch
    the running program).
- **Receiver** = infrared *receiver* entity. Captured timing signals are
  decoded into MWM frames locally and exposed as diagnostic sensors:
  - *last message*: raw frame hex plus our decoded interpretation,
  - *assumed state*: best-effort inference of what nearby ears are doing,
    aggregated across ALL receivers and transmitters in the room,
  - *message count*: traffic volume since start.

## Room-level awareness

Every receiver feeds one shared hub. When it hears commands we did not
send -- wand pushes, another transmitter, idle beacons from ear hats --
the light entities reflect what was understood of them, and periodic
repetition suspends until you act again (we don't fight other controllers
for the room). Our own transmissions echoed back within a short window are
recognised as such and ignored for this purpose.

## Development

Tests use the stdlib only (`unittest`; homeassistant is not installed in
this environment). The vendored library and the pure state logic in
`ears.py` (refresh policy, ours-vs-foreign discrimination, suspension)
are fully unit-tested; the HA platform modules are syntax-checked.

```
make test-python   # from repo root, or:
cd python && PYTHONPATH=. python3 -m unittest discover -s tests -v
```

| Path | Purpose |
|------|---------|
| `_mwm/` | protocol library (framing, timings codec, decoder, palette) |
| `ears.py` | pure state logic: pair desired-state, refresh/suspend rules, observed-traffic hub |
| `tests/` | unittest suite for both |
| remaining modules | HA glue: config flow, light/sensor platforms |

`tests/_bootstrap.py` loads `_mwm` standalone by path, and `ears.py`
falls back to the same alias when loaded outside a package, keeping
everything testable without homeassistant installed.
