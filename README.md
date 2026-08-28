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

- **Infrared emitter** -> exposes *Both Ears*, *Left Ear* and *Right
  Ear* lights driving every MWM ear in range as one paired set (all
  three share a device and always agree -- they are views over one
  room-state store). *Both Ears* maps to the protocol's native both-ear
  frames; *Right Ear* to the right-only frames; *Left Ear* is the
  composed proxy (both -> left colour, then right-only restore).
  - Any colour from HA's picker snaps to the nearest representable shade
    using hue-dominant matching (hue drift is penalised far more than
    brightness drift, so dark/muted requests stay in family instead of
    leaping to a bright neighbour), then transmits ONLY verified corpus
    frames (samples/mwm-gwts-colors.tsv;
    rig session 2026-08-23 confirmed multi-byte phrases are sequential
    opcode scripts -- last opcode wins on both ears -- so no per-side
    fused phrases are ever invented):
    - simple colours: both-ears `90 6X`, right-only `90 68+X`. Right-slot
      changes send the single right-only form (no visible flash); left
      changes compose `90 <left>` + `90 <right-only restore>`.
    - palette shades: both-ears `91 0E pp`, right-only `91 0E pp|80`.
      Left picks compose both+restore when the right ear already holds a
      palette shade, else degrade to the both-ears form.
    - equal pairs use just the canonical single frame (incl. `90 60` off).
    No leading `24` override precedes colour writes: it blacks the ears
    for seconds and the canonical frames land without it (rig-verified).
    Effect invocation still leads with `24` (doc: required to escape a
    running program). Each logical command re-transmits as a GROUP --
    frames back-to-back, two passes ~1.8 s apart, mirroring the proven
    `ir-mwm-send` recipe for receivers that drop cold single frames.
  - Effects appear in HA's drop-down (the entity declares EFFECT
    support) and apply to the ears as a whole -- the protocol has no
    per-ear effect invocation. Effect picks invoke verified `48 XX`
    programs. Bare toggles re-issue the remembered colour, defaulting
    to white.
  - Lights follow the room: an overheard FOREIGN command (wand or other
    transmitter) is adopted into the displayed state and pauses our
    repeats until your next action; idle beacons keep the effect label
    current without touching anything.
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

## Rig research tools

The rig research tools under `../tools/` drive and analyse real ears on
the MQTT test rig, standalone (no Home Assistant). Full documentation is
in `../README.md`; the shared `_bootstrap.py` / `_mqtt.py` helpers and
their usage are described there. The set includes interactive senders,
beacon capture/analysis, colour-cycle testing, and offline log decoding.

### colour cycle test (`../tools/color_cycle.py`)

Sends every simple colour, palette shade, composite frame, and built-in
effect one at a time via MQTT (Tasmota IRsend), prompting for free-form
notes after each command. Results are logged to a JSON file for later
analysis.

Each command is sent **3 times** (configurable) with a short delay
between repeats so the human observer does not miss the change on real
ears.

```
python3 ../tools/color_cycle.py                       # defaults: repeat=3, delay=0.3s
python3 ../tools/color_cycle.py --repeat 5 --repeat-delay 0.5
python3 ../tools/color_cycle.py --log session.json
```

| Flag | Default | Description |
|------|---------|-------------|
| `--log FILE` | `color_cycle_YYYYMMDD_HHMMSS.json` | Output log path |
| `--mqtt-json PATH` | `~/.config/ir-remote-tools/mqtt.json` | MQTT config |
| `--repeat N` | `3` | Send each command N times for human visibility |
| `--repeat-delay SECS` | `0.3` | Seconds between repeated sends |

The log is a JSON object with an `entries` array; each entry has `t`,
`kind`, `name`, `hex`, `payload`, `repeat`, and `notes` fields.

### offline analysis (`../tools/analyze_log.py`)

Parses a log produced by `color_cycle.py` (or a manually assembled
equivalent) and attempts to **decipher every observed command**. Emits a
structured analysis log with full decoding where possible, or a note
that the command could not be fully decoded.

Supports three input modes:

- `--log FILE` -- a JSON log from `color_cycle.py`
- `--frames FILE` -- plain-text hex frame strings, one per line
  (blank lines and `#` comments ignored)
- `--timings FILE` -- a JSON file containing a top-level list of
  integer timing arrays (each in microseconds, as received from the
  Tasmota MQTT receiver topic)

```
python3 ../tools/analyze_log.py --log color_cycle.json
python3 ../tools/analyze_log.py --log color_cycle.json --format json --out analysis.json
python3 ../tools/analyze_log.py --frames captures.txt
python3 ../tools/analyze_log.py --timings raw_captures.json --format text
```

| Flag | Default | Description |
|------|---------|-------------|
| `--log FILE` | | JSON log from `color_cycle.py` |
| `--frames FILE` | | Plain-text hex frame strings |
| `--timings FILE` | | JSON timing arrays |
| `--format text\|json` | `text` | Output format |
| `--out FILE` | stdout | Output file |

Each record in the output contains the raw input, decoded frame
description (kind, summary, tokens), and bundle analysis where
applicable. Commands that cannot be decoded are flagged with
`[NO DECODE]` or `[INVALID]` so they are easy to find in a long session.
