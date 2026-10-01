"""Structural contract between the setup glue and the platforms.

The platform modules import homeassistant and therefore cannot be loaded
in this environment -- which is exactly how the v0.3 runtime-shape bug
(platforms fetching EarPairState/ReceiverData directly after __init__
switched to a {"pair": ..., "rx": ...} dict) shipped despite a green
suite. These greps pin the contract instead.
"""

import json
import pathlib
import re
import unittest

import yaml

import _bootstrap  # noqa: F401  (must precede mwm imports)

from mwm import LIGHT_EFFECTS

_COMPONENT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components" / "mwm_ears"
)
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (_COMPONENT / name).read_text()


class RuntimeShapeContract(unittest.TestCase):
    def test_light_reads_pair_key_from_runtime_dict(self):
        src = _read("light.py")
        self.assertIn('runtime["pair"]', src)
        self.assertNotRegex(
            src, r"hass\.data\[DOMAIN\]\[entry\.entry_id\]\s*$"
        )

    def test_sensor_reads_rx_key_from_runtime_dict(self):
        src = _read("sensor.py")
        self.assertIn('runtime["rx"]', src)
        self.assertNotRegex(
            src, r"hass\.data\[DOMAIN\]\[entry\.entry_id\]\s*$"
        )

    def test_init_stores_the_runtime_dict(self):
        src = _read("__init__.py")
        self.assertIn('{"pair": None, "rx": None}', src)

    def test_config_flow_uses_the_conf_symbols(self):
        flow = _read("config_flow.py")
        const = _read("const.py")
        conf_symbols = re.findall(r"^(CONF_[A-Z_]+) =", const, re.M)
        self.assertTrue(conf_symbols, "const.py lost its CONF_* keys?")
        for symbol in conf_symbols:
            self.assertIn(
                symbol, flow, f"{symbol} defined but unused by config_flow"
            )


if __name__ == "__main__":
    unittest.main()


class BootReadinessContract(unittest.TestCase):
    def test_setup_raises_not_ready_for_undiscovered_entities(self):
        src = _read("__init__.py")
        self.assertIn("ConfigEntryNotReady", src)
        self.assertRegex(src, r"_entity_discovered\(hass,")
        self.assertNotRegex(
            src,
            r"hass\.data\[DOMAIN\]\[entry\.entry_id\] = runtime[\s\S]*?"
            r"ConfigEntryNotReady",
            "runtime dict must be stored only AFTER the readiness gate",
        )


class EntityNamingContract(unittest.TestCase):
    def test_every_sensor_class_has_a_descriptive_name(self):
        src = _read("sensor.py")
        classes = re.findall(r"^class (Mwm\w+)\(", src, re.M)
        self.assertEqual(
            len(classes), 4,
            f"unexpected sensor class list: {classes}",
        )
        for cls in classes:
            body = re.search(
                rf"class {cls}\([\s\S]*?(?=\nclass |\Z)", src
            ).group(0)
            has_name = "def name(self)" in body or "_attr_name" in body
            self.assertTrue(has_name, f"{cls} lacks a name")


class ResilientSetupContract(unittest.TestCase):
    """Setup must survive device offline/online cycles without manual reload."""

    def test_entity_usable_checks_state_not_availability(self):
        src = _read("__init__.py")
        self.assertIn("def _entity_usable(", src)
        self.assertIn("STATE_UNAVAILABLE", src)
        self.assertIn("STATE_UNKNOWN", src)

    def test_entity_usable_allows_unknown_transmitter(self):
        """Tasmota IR transmitters rest at 'unknown' (they never publish state).

        Blocking unknown would silently drop every transmission from any
        Tasmota-bound room, so _entity_usable must only gate on UNAVAILABLE.
        """
        src = _read("__init__.py")
        self.assertNotRegex(
            src,
            r"state\.state not in \(STATE_UNAVAILABLE,\s*STATE_UNKNOWN\)",
            "_entity_usable must not exclude unknown transmitters",
        )

    def test_entity_discovered_checks_state_exists(self):
        src = _read("__init__.py")
        self.assertIn("def _entity_discovered(", src)

    def test_setup_tracks_receiver_state_changes(self):
        src = _read("__init__.py")
        self.assertIn("async_track_state_change_event", src)
        self.assertIn("_on_receiver_state", src)

    def test_setup_does_not_raise_for_unavailable_entities(self):
        src = _read("__init__.py")
        self.assertNotRegex(
            src,
            r"_entity_usable\(hass,.*\n.*raise ConfigEntryNotReady",
            "must not raise ConfigEntryNotReady for unavailable entities",
        )

    def test_transmit_guards_unavailable_emitter(self):
        src = _read("__init__.py")
        self.assertIn("_entity_usable(hass, emitter_entity)", src)
        self.assertIn("_LOGGER.warning(", src)
        self.assertIn('"emitter %s unavailable (%s), dropping IR transmissions"', src)

    def test_play_show_rejects_unavailable_emitter(self):
        src = _read("light.py")
        self.assertIn("playback not started", src)
        self.assertIn("STATE_UNAVAILABLE", src)

    def test_play_show_rejects_when_already_running(self):
        src = _read("light.py")
        self.assertIn("player.playing", src)
        self.assertIn("already running", src)
        self.assertIn("stop_show first", src)

    def test_watchdog_skips_when_entity_unavailable(self):
        src = _read("__init__.py")
        self.assertIn("STATE_UNAVAILABLE", src)
        self.assertIn("STATE_UNKNOWN", src)
        self.assertRegex(
            src,
            r"state\.state in.*STATE_UNAVAILABLE",
            "watchdog must check entity availability",
        )


class OptionsFlowContract(unittest.TestCase):
    """Options flow must allow re-selecting emitter/receiver entities."""

    def test_options_flow_reads_emitters(self):
        src = _read("config_flow.py")
        self.assertIn("async_get_emitters", src)

    def test_options_flow_reads_receivers(self):
        src = _read("config_flow.py")
        self.assertIn("async_get_receivers", src)

    def test_options_flow_updates_entry_data(self):
        src = _read("config_flow.py")
        self.assertIn("async_update_entry", src)
        self.assertIn("async_reload", src)


class SpacedRepeatContract(unittest.TestCase):
    """TX must space-repeat like ir-mwm-send; framework repeats don't land."""

    def test_transmit_closure_spaces_extra_repeats(self):
        src = _read("__init__.py")
        self.assertIn("asyncio.sleep(REPEAT_GAP_S)", src)
        self.assertIn("range(repeat_count + 1)", src)
        # framework-level repeats must be neutralised
        self.assertRegex(src, r"MwmCommand\(\s*frame,\s*repeat_count=0")

    def test_gap_constant_is_documented(self):
        src = _read("const.py")
        self.assertIn("REPEAT_GAP_S", src)
        self.assertIn("drop cold single frames", src)


class TuyaS18CompensationContract(unittest.TestCase):
    """Space compensation is keyed to the emitter's firmware identity.

    Tuya-firmware S18 IR blasters compress every space by a fixed offset; the
    compensation must apply to exactly that firmware (tuya_local +
    basic_ir_remote driver) and to nothing else -- not every room, not the
    entity id, not the platform alone.
    """

    def test_transmit_applies_resolved_compensation(self):
        src = _read("__init__.py")
        # resolve once at setup, into runtime (so the switch can toggle it)
        self.assertIn("_space_compensation_us(hass, emitter_entity)", src)
        self.assertIn('runtime["space_comp_us"]', src)
        self.assertIn("space_stretch_us=runtime[", src)

    def test_detection_keys_on_platform_and_device_type(self):
        src = _read("__init__.py")
        self.assertIn("ent.platform != TUYA_S18_PLATFORM", src)
        self.assertIn('owner.data.get("type") != TUYA_S18_DEVICE_TYPE', src)
        # identity is positively identified; anything unknown defaults to 0
        self.assertIn("return 0", src)

    def test_constants_identify_the_s18_firmware(self):
        const = _read("const.py")
        self.assertIn('TUYA_S18_PLATFORM = "tuya_local"', const)
        self.assertIn('TUYA_S18_DEVICE_TYPE = "basic_ir_remote"', const)
        self.assertIn("TUYA_S18_SPACE_COMP_US", const)

    def test_switch_exists_only_when_compensation_detected(self):
        src = _read("switch.py")
        self.assertIn("MwmS18CompensationSwitch", src)
        # no switch on emitters that need no compensation
        self.assertIn('runtime.get("space_comp_available_us", 0)', src)

    def test_switch_toggles_between_full_and_zero(self):
        src = _read("switch.py")
        self.assertIn('self._runtime["space_comp_available_us"]', src)
        self.assertIn('self._runtime["space_comp_us"] = 0', src)


class VersionContract(unittest.TestCase):
    """manifest.json and const.py must agree on the integration version.

    HA reads the version from manifest.json (update detection), while the
    device page shows sw_version sourced from const.py; a silent drift
    between them would make update tracking lie.
    """

    def test_manifest_version_matches_const(self):
        import json

        manifest = json.loads(_read("manifest.json"))
        const = _read("const.py")
        match = re.search(
            r'^INTEGRATION_VERSION = "([^"]+)"', const, re.M
        )
        self.assertIsNotNone(match, "const.py lost INTEGRATION_VERSION")
        self.assertEqual(manifest.get("version"), match.group(1))


class VendoredLibraryContract(unittest.TestCase):
    """The committed _mwm/ copy must be the pinned python-mwm release.

    `make vendor` records what it pulled in only as the library's own
    __version__, so a pull that half-happened (or a pin bumped without
    re-vendoring) would otherwise ship silently -- which is how _mwm/ sat at
    0.1.0 while the library was at 0.3.0.
    """

    def test_vendored_version_matches_pinned_tag(self):
        import mwm

        tag = re.search(
            r'^_MWM_TAG = "([^"]+)"',
            (_REPO_ROOT / "tools" / "vendor_mwm.py").read_text(), re.M,
        )
        self.assertIsNotNone(tag, "tools/vendor_mwm.py lost the _MWM_TAG pin")
        self.assertEqual(
            mwm.__version__, tag.group(1).lstrip("v"),
            "vendored _mwm/ is not the pinned python-mwm release; "
            "run `make vendor`",
        )


class SetStateServiceContract(unittest.TestCase):
    """mwm_ears.set_state must be a complete, documented automation surface.

    light.py cannot be imported here (homeassistant is absent), so these
    tests pin its contract against services.yaml and the shared catalog.
    """

    def _services(self) -> dict:
        return yaml.safe_load(_read("services.yaml"))

    def test_set_state_declared_with_all_fields(self):
        svc = self._services()["set_state"]
        self.assertIsNotNone(svc.get("target"))
        for field in ("color", "left_color", "right_color", "effect"):
            self.assertIn(field, svc.get("fields", {}), f"set_state lost {field!r}")

    def test_effect_selector_matches_catalog(self):
        svc = self._services()["set_state"]
        options = svc["fields"]["effect"]["selector"]["select"]["options"]
        self.assertEqual(sorted(options), sorted(LIGHT_EFFECTS))

    def test_effect_catalog_has_no_duplicate_indices(self):
        indices = list(LIGHT_EFFECTS.values())
        self.assertEqual(len(indices), len(set(indices)))

    def test_light_platform_uses_the_shared_catalog(self):
        light = _read("light.py")
        self.assertNotIn("LIGHT_EFFECTS: dict[str, int] = {", light)
        self.assertIn("LIGHT_EFFECTS", light)
        self.assertIn('hass.services.async_register(DOMAIN, "set_state"', light)
        self.assertIn('hass.services.async_register(DOMAIN, "select_color"', light)

    def test_strings_declare_set_state_fields(self):
        strings = json.loads(_read("strings.json"))
        service = strings["services"]["set_state"]
        for field in ("color", "left_color", "right_color", "effect"):
            self.assertIn(field, service["fields"], f"strings.json lost {field!r}")


class PlayShowServiceContract(unittest.TestCase):
    """mwm_ears.play_show/stop_show must stay a complete documented surface."""

    def _services(self) -> dict:
        return yaml.safe_load(_read("services.yaml"))

    def test_play_show_declared_with_all_fields(self):
        svc = self._services()["play_show"]
        self.assertIsNotNone(svc.get("target"))
        for field in ("file", "script", "repeat", "reset"):
            self.assertIn(field, svc.get("fields", {}), f"play_show lost {field!r}")

    def test_stop_show_declared_with_target(self):
        self.assertIsNotNone(self._services()["stop_show"].get("target"))

    def test_light_platform_registers_show_services(self):
        light = _read("light.py")
        self.assertIn('hass.services.async_register(DOMAIN, "play_show"', light)
        self.assertIn('hass.services.async_register(DOMAIN, "stop_show"', light)

    def test_strings_declare_play_show_fields(self):
        strings = json.loads(_read("strings.json"))
        for field in ("file", "script", "repeat", "reset"):
            self.assertIn(field, strings["services"]["play_show"]["fields"])

    def test_player_wired_to_pair_and_cancelled_on_unload(self):
        src = _read("__init__.py")
        self.assertIn("ShowPlayer(pair.replay_frame)", src)
        self.assertIn("pair.show_player = player", src)
        self.assertIn("entry.async_on_unload(player.stop)", src)


class EffectsSelectorContract(unittest.TestCase):
    """Effect programs are room-wide: only the Ears entity offers them."""

    def test_effect_list_gated_on_both_side(self):
        light = _read("light.py")
        body = re.search(
            r"def effect_list\(self\)[\s\S]*?(?=\n    @property|\n    def )",
            light,
        ).group(0)
        self.assertRegex(body, r"if self\._side != BOTH:")
        self.assertIn("return None", body)
