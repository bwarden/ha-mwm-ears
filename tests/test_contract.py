"""Structural contract between the setup glue and the platforms.

The platform modules import homeassistant and therefore cannot be loaded
in this environment -- which is exactly how the v0.3 runtime-shape bug
(platforms fetching EarPairState/ReceiverData directly after __init__
switched to a {"pair": ..., "rx": ...} dict) shipped despite a green
suite. These greps pin the contract instead.
"""

import pathlib
import re
import unittest

_COMPONENT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components" / "mwm_ears"
)


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
    def test_setup_raises_not_ready_for_missing_entities(self):
        src = _read("__init__.py")
        self.assertIn("ConfigEntryNotReady", src)
        self.assertRegex(src, r"_entity_ready\(hass, e\)")
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
            self.assertIn("def name(self)", body, f"{cls} lacks a name")


class SpacedRepeatContract(unittest.TestCase):
    """TX must space-repeat like ir-mwm-send; framework repeats don't land."""

    def test_transmit_closure_spaces_extra_repeats(self):
        src = _read("__init__.py")
        self.assertIn("asyncio.sleep(REPEAT_GAP_S)", src)
        self.assertIn("range(repeat_count + 1)", src)
        # framework-level repeats must be neutralised
        self.assertRegex(src, r"MwmCommand\(frame, repeat_count=0\)")

    def test_gap_constant_is_documented(self):
        src = _read("const.py")
        self.assertIn("REPEAT_GAP_S", src)
        self.assertIn("drop cold single frames", src)
