# Changelog

All notable changes to the mwm_ears Home Assistant integration.
Releases are tagged `v<version>`; version numbers live in
`manifest.json` and `const.py` in lockstep.

## [0.8.3] - 2026-09-26

### Setup

- **Declared the `http` and `lovelace` dependencies** — both are used
  during `async_setup_entry` (the card's static path is registered through
  `hass.http`, then the Lovelace resource collections are read and written),
  but neither was in `dependencies`. Hassfest caught it on the first public
  run of the Validate workflow:

      [ERROR] [DEPENDENCIES] Using component http but it's not in 'dependencies' or 'after_dependencies'
      [ERROR] [DEPENDENCIES] Using component lovelace but it's not in 'dependencies' or 'after_dependencies'

  In practice, when Lovelace was not set up yet, the card registration hit
  the "Lovelace not loaded yet" branch in `__init__.py` and was deferred to
  a later setup -- so an install could come up with no dashboard card until
  something forced a reload. `dependencies` (not `after_dependencies`) is the
  correct list here: both touches happen during our own setup, so they must
  be set up *before* us. The defensive `try`/`except` and the deferral
  branch stay, since they still cover YAML resource mode and a failed
  static-path registration.
- **Dropped the dead `homeassistant` key from the manifest** — hassfest
  rejected the whole manifest on it:

      [ERROR] [MANIFEST] Invalid manifest: not a valid option, did you mean 'homekit'? at 'homeassistant'. Got '2026.4.0'

  It is not a key in HA's integration manifest schema and the runtime loader
  ignores it entirely. The minimum-version statement is unchanged in
  substance: it lives in `hacs.json`'s `homeassistant`, which HACS does
  enforce against the running instance.

## [0.8.2] - 2026-09-25

### Vendored library

- **Re-vendored from the published `python-mwm` release** — `_mwm/` now
  carries python-mwm `v0.3.0`, the published tag that the library's own
  docs define as the pin for vendored consumers. Only the version stamp
  moved: the six protocol modules were already byte-identical to `v0.3.0`,
  so there is no protocol behaviour change in this release. `_mwm/\_\_init\_\_.py`
  had been left stamped `0.1.0` while its modules already held `0.3.0`
  content, so nothing in the integration could say which release it was
  built from.
- **Vendoring pins the tag, not a sibling checkout** — `make vendor`
  shallow-clones `github.com/bwarden/python-mwm` at the tag recorded in
  `tools/vendor_mwm.py` (`_MWM_TAG`) into a gitignored `.mwm/` cache, so a
  release is reproducible from the published repo alone. `make build` and
  `make test` vendor only when that cache (or an `MWM_SRC` override) already
  exists, so a fresh checkout and CI test the committed copy without
  network access. A cache left on a stale tag is replaced, not reused.
- **New contract test** — `VendoredLibraryContract` fails when the committed
  `_mwm/` copy is not the release the pin names, so a half-landed pull
  cannot ship again.

## [0.8.1] - 2026-09-20

### Vendored library

- **`unbundle` capability** — the vendored `_mwm` library ships
  `mwm.protocol.unbundle`, the byte-identical port of the TS
  `MwmProtocol.unbundle` capability: it splits a whole A+B+A' capture
  (Tasmota / IRremoteESP8266 commit 247bcdb3 logs it as one Data value)
  into its own frames via each frame's self-declared length nibble.
  `ObservedHub.ingest` already collapses a frame-level A+B+A' capture to
  its phrase; that triplet-unwrap is now documented as mirroring the
  capability's first-frame law, and the value-level raw path is noted as
  intentionally absent.

## [0.8.0] - 2026-09-18

### Added

- **`.msh` show replay** — `mwm_ears.play_show` replays a captured show
  script (file or inline) frame-by-frame through the targeted rooms'
  `infrared` transmitter entities (MQTT or ESPHome), matching
  `tools/mwm-send.py`'s wire behaviour; `mwm_ears.stop_show` cancels.
  Each show is planned with the same min-gap clamp, GO-anchoring and
  end-of-show all-off reset, and `show_playing`/`show_name` attributes
  report playback state.
- **Multi-room service dispatch** — `set_state`, `select_color`,
  `play_show` and `stop_show` address every ear light entity in the
  target, transmitting once per room (was: first room only).
- **Vendored library updates** — demo-mode beacon catalogue
  (`DEMO_BEACONS` / `demo_beacon_label`), cascade phrase builders
  (`build_cascade`, `build_sparse_cascade`, `CASCADE_DELAYS`) and `cue`
  decode coverage.

### Changed

- **Sensor state cap** — the `last phrase` sensor truncates to Home
  Assistant's 255-character limit (252 + `...`); the full capture remains
  on the `all_frames` attribute.
- **Emitter availability** — IR transmitters resting at `unknown` (a
  healthy Tasmota emitter never publishes a state) are now usable; only
  `unavailable`, or not yet discovered, blocks a transmission. A dropped
  emitter warns once per lapse instead of per frame.

### Fixed

- Entity labelling used the wrong registry API and raised
  `AttributeError: 'EntityRegistry' object has no attribute 'async_update'`;
  now uses `async_update_entity`.

### Card

- The Left / Both / Right palettes sit side-by-side as fixed 7-wide grids
  so the 7 one-bit colours always fill the top row and the 30 shades flow
  below; swatch names become hover chips at this density.

## [0.7.7] - 2026-09-02

- HACS-installable integration with HACS/Hassfest CI.
- Card live vs batch (queued) Transmit mode, per-ear Off circles, and
  effect fixes (effects act on current colour; versioned card resource
  cache-busting).