# Home Assistant integration: `mwm_ears`

Controls Disney "Made With Magic" / Glow With The Show ears through Home
Assistant's **infrared entity platform** (HA 2026.4+). Requires at least
one infrared proxy configured in HA (e.g. an ESPHome IR/RF proxy); the
integration is a *consumer* on that platform, not a hardware transport
itself.

> **Disclaimer:** Independent community project for interoperability with
> independently purchased hardware. Not supplied by, authorized by,
> affiliated with, or endorsed by Disney. "Made With Magic", "Glow With The
> Show", and all related names and marks are trademarks of their respective
> owners.

The component is self-contained: the MWM protocol library is vendored
under `custom_components/mwm_ears/_mwm/` (framing, CRC-8/Dallas,
palette tables, phrase decoder, timing codec). The only external
requirement is `infrared-protocols`, pulled in via the manifest for the
framework's `Command` envelope type; MWM support itself lives here.

## Install

Unpack the distribution into your HA config directory:

```
unzip dist/mwm_ears.zip -d ~/.homeassistant/
```

(or copy `custom_components/mwm_ears/` by hand). Restart Home
Assistant, then add a room via Settings -> Devices & Services -> Add
Integration -> "MWM Ears". One integration instance represents ONE MWM
room and binds two infrared entities -- usually two halves of the same IR
box, which the form suggests together; either half is optional:

- **Infrared emitter** -> exposes *Left Ear* and *Right Ear* lights
  driving every MWM ear in range as a paired set.
  - Any colour from HA's picker snaps to the nearest representable shade:
    simple colours go per-ear by composing the two verified primitives --
    `90 <left>` brings both ears to the left colour, then the right-only
    form (`90 68+X`) restores the right ear (equal pairs use just the
    canonical `90 6X` form). Multi-byte phrases are NOT per-side fuses:
    their opcodes execute in order against both ears, so an embedded off
    byte eventually blacks everything (rig session 2026-08-23). Palette
    shades use the verified both-ears template (no per-ear form).
    Every colour write is preceded by the standalone `24` flow-control
    override, which per the protocol doc is required to take effect while
    a built-in program runs (cost: a momentary black dip). Composed state
    groups re-transmit whole (frames back-to-back), 1 + N group passes
    spaced ~1.8 s apart -- ear receivers drop
    cold single frames, and genuine wands repeat likewise (mirrors the
    proven `ir-mwm-send` recipe).
  - Effect list invokes verified `48 XX` programs (fades, pulses, strobe,
    transitions, rotations, random, blackout). Bare toggles re-issue the
    side's last colour, defaulting to white.
  - While both ears are on, the current phrase is re-issued every ~8 s so
    newly-powered ears join in. **Off is never repeated** beyond its
    initial multi-burst; effect invocations likewise fire once.
- **Infrared receiver** -> captured timing signals are decoded locally
  (end-bit-swallowed bytes recovered by CRC brute-force) into diagnostic
  sensors:
  - *last phrase*: message A of the most recent capture (the whole capture
    when it wasn't a bundle), with our decoded interpretation,
  - *last companion*: message B of an A-B-A' bundle -- effect/cycle/clock
    parameters; state is `none` unless the last capture was a bundle. A'
    duplicates A and is not reported separately,
  - *assumed state*: best-effort inference of what nearby ears are doing,
    aggregated across ALL receivers and transmitters in the room,
  - *message count*: traffic volume since start.

Both halves' entities live on one Home Assistant device, whether the
emitter and receiver are the same physical box or not. At startup the
integration waits for the bound infrared entities to exist (retrying with
backoff), so MQTT-discovered IR boxes that appear late simply delay the
room instead of requiring a manual reload. Diagnostic sensors carry
descriptive names (Last Phrase, Last Companion, Assumed Ear State,
Message Count) rather than sharing the room name. Entries created
before v0.3 (per-kind transmitter/receiver bindings) cannot migrate;
delete and re-add them.

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
