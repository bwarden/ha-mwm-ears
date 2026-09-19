# Changelog

All notable changes to the mwm_ears Home Assistant integration.
Releases are tagged `v<version>`; version numbers live in
`manifest.json` and `const.py` in lockstep.

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