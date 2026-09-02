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

- **Infrared emitter** -> exposes *Ears*, *Left Ear* and *Right Ear*
  lights driving every MWM ear in range as one paired set (all
  three share a device and always agree -- they are views over one
  room-state store). *Ears* maps to the protocol's native both-ear
  frames; *Right Ear* to the right-only frames; *Left Ear* is the
  composed proxy (both -> left color, then right-only restore).
  *Ears* is on when either ear is on, so turning it off turns both off.
  - Any color from HA's picker snaps to the nearest representable shade
    using hue-dominant matching (hue drift is penalised far more than
    brightness drift, so dark/muted requests stay in family instead of
    leaping to a bright neighbour), then transmits ONLY verified corpus
    frames (samples/mwm-gwts-colors.tsv;
    rig session 2026-08-23 confirmed multi-byte phrases are sequential
    opcode scripts -- last opcode wins on both ears -- so no per-side
    fused phrases are ever invented):
    - simple colors: both-ears `90 6X`, right-only `90 68+X`. Right-slot
      changes send the single right-only form (no visible flash); left
      changes compose `90 <left>` + `90 <right-only restore>`.
    - palette shades: both-ears `91 0E pp`, right-only `91 0E pp|80`.
      Left picks compose both+restore when the right ear already holds a
      palette shade, else degrade to the both-ears form.
    - equal pairs use just the canonical single frame (incl. `90 60` off).
    No leading `24` override precedes color writes: it blacks the ears
    for seconds and the canonical frames land without it (rig-verified).
    Effect invocation still leads with `24` (doc: required to escape a
    running program). Each logical command re-transmits as a GROUP --
    frames back-to-back, two passes ~1.8 s apart, mirroring the proven
    `ir-mwm-send` recipe for receivers that drop cold single frames.
  - Effects appear in HA's drop-down (the entity declares EFFECT
    support) and apply to the ears as a whole -- the protocol has no
    per-ear effect invocation. Effect picks invoke verified `48 XX`
    programs. Bare toggles re-issue the remembered color, defaulting
    to white.
  - By default the lights follow the room (passive): an overheard FOREIGN
    command (wand or other transmitter) is adopted into the displayed
    state and pauses our repeats until your next action; idle beacons keep
    the effect label current without touching anything. **Off is never
    repeated** beyond its initial multi-burst; effect invocations likewise
    fire once.
  - The *Enforce Ears* switch (one per room) flips this to sole control:
    when ON, the integration takes over: it re-asserts the light-entity
    state immediately any time it hears a foreign MWM phrase (beacon, wand,
    or other transmitter) rather than adopting it -- ears are pulled back
    onto your state, held OFF or ON. On the periodic timer: an enforced ON
    is re-asserted every ~10 s (to hold color against demo/standalone
    drift), while an enforced OFF backs off to every ~90 s -- just under
    the ~2 min after which ears fall back into demo mode -- so they stay
    dark without flogging the IR bus. Foreign commands are overridden rather
    than adopted; our own echoes are ignored. A beacon that proves the ears
    physically alive never moves an enforcing room's light entity either --
    the held command is the whole truth for its display, so a held OFF stays
    showing off even while the ears demo-beacon beneath the re-assert. When
    the switch is switched ON, whatever the room is *currently* showing
    (including a state a foreign wand just drove) becomes the held state, so
    the first re-assert never overrides the live display with a stale
    command. **Enforcement never assumes off**. Defaults to OFF (passive).
  - The assume-off silence rule applies only while NOT enforcing: if no
    beacon is heard for ~180 s (3 min) while a light is ON (and the enforce
    switch is off), we assume the ears powered off and the lights reflect
    off. The window exceeds the ~2 min of silence ears naturally show after
    a command before they begin demo-beaconing again, so it can't misfire on
    an on-but-quiet pair. A room that is enforcing instead always re-asserts
    its target state.
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

## Lovelace card

A Lovelace custom card is bundled with the integration
(`custom_components/mwm_ears/frontend/mwm-ears-card.js`) for controlling the
ears from the dashboard. It shows three palettes — **Left Ear**, **Both
Ears**, **Right Ear** — with every representable color, plus an **effects**
picker, all driven through the integration's `light.*` entities so each pick
emits the correct, verified IR frames (color snapping, per-side composition,
and effect invocation are all handled by the integration).

### Install

The card ships inside the integration and is served straight from its install
path, so installing the component is enough — no manual copy into `www/` and
no separate resource registration:

1. Install the integration (HACS, or unpack the release zip into
   `custom_components/mwm_ears/`) and restart HA.
2. Nothing else. On storage-mode dashboards (the default) the integration
   registers the card module itself as a Lovelace resource, using a card URL
   versioned with the integration version
   (`…/mwm-ears-card.js?v=<version>`). When an updated integration is
   installed the registered URL changes, so the frontend fetches the new card
   instead of serving a stale one from the browser/app cache.

*YAML-only resource mode:* an integration cannot edit YAML resources. Add the
entry by hand, using a `?v=` suffix matching your installed version (the
integration logs the exact URL at startup):

```yaml
resources:
  - url: /custom_components/mwm_ears/frontend/mwm-ears-card.js?v=0.7.4
    type: module
```

If static serving is unavailable on your install (the log prints a warning),
fall back to copying `frontend/mwm-ears-card.js` into your HA `www/`
directory (e.g. `www/community/mwm_ears/mwm-ears-card.js`) and register the
resource URL `/local/community/mwm_ears/mwm-ears-card.js` instead. You can
also use *JavaScript* mode (paste the file contents) in either flow.

### Configure

A card element (via the visual editor, or YAML):

```yaml
type: custom:mwm-ears-card
entity: light.ears            # the "Ears" (Both) light — required
left_entity: light.left_ear   # optional
right_entity: light.right_ear # optional
title: My Ears
```

- `entity` is the Both-ear light; it drives the "Both Ears" palette and the
  effects picker.
- `left_entity` / `right_entity` are optional. The card auto-detects them as
  the `side`-stamped lights that share the Both entity's Home Assistant
  device (the integration registers the Left/Both/Right lights on one device
  per room), so `entity` alone is enough on a fresh install. Set them only to
  override the detection. A side whose entity is neither configured nor
  detected is shown read-only.
- The swatches come from the live `color_palette` entity attribute
  (`python/.../_mwm/palette.py::color_palette`), so the card always shows
  exactly the colors the integration can represent — no palette copy in JS.

### Behavior

- Clicking a swatch calls the integration's `mwm_ears.select_color` action
  with the exact catalog selector (`simple:0x61`, `palette:4`) on the
  matching side entity. The action sends the selected shade **exactly** —
  bypassing color-wheel snapping — so near-identical shades that are
  distinct protocol commands (simple `0x61` blue vs palette `0x04` pure
  blue) can be sent and tested individually. The active swatch is matched by
  the entity's `color_identity` attribute (kind + code/index), never by RGB.
- Picking from the HA color wheel still snaps to the nearest shade, and
  palette shades that look identical to a one-bit simple color (e.g. lime
  green → simple green) resolve to the simple opcode, whose right-only
  primitive is a single command.
- **On / Off** control the whole pair (the Both entity). **Restore color**
  clears a running effect and re-issues the remembered color (a bare
  `turn_on`); it does not turn the ears off.
- The **Effects** picker runs a room-wide effect program via the Both entity
  (effect programs are not per-ear).

The `select_color` action is also available from HA's Actions developer
tool, scripts, and automations. Its `color` field accepts a catalog name
(`"lime green"`, `"pure blue"`, case-insensitive, tolerating spelling
variants such as `gray`/`grey`) or a `"kind:value"` selector
(`"simple:0x61"`, `"palette:4"`, or `"palette:white"`), and targets any of
the three side entities.

## Room-level awareness

Every receiver feeds one shared hub. When it hears commands we did not
send -- wand pushes, another transmitter, idle beacons from ear hats --
the light entities reflect what was understood of them, and periodic
repetition suspends until you act again (we don't fight other controllers
for the room). Our own transmissions echoed back within a short window are
recognized as such and ignored for this purpose.

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
| remaining modules | HA glue: config flow, light/sensor/switch platforms |

`tests/_bootstrap.py` loads `_mwm` standalone by path, and `ears.py`
falls back to the same alias when loaded outside a package, keeping
everything testable without homeassistant installed.

## Rig research tools

The rig research tools under `../tools/` drive and analyse real ears on
the MQTT test rig, standalone (no Home Assistant). Full documentation is
in `../README.md`; the shared `_bootstrap.py` / `_mqtt.py` helpers and
their usage are described there. The set includes interactive senders,
beacon capture/analysis, color-cycle testing, and offline log decoding.

### color cycle test (`../tools/color_cycle.py`)

Sends every simple color, palette shade, composite frame, and built-in
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
