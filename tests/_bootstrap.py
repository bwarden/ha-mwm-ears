"""Expose the integration's vendored protocol library as `mwm`.

The custom component's package __init__ imports homeassistant, which is not
installed in this environment. Loading the vendored _mwm subpackage directly
by path keeps the component idiomatic while making its library testable.
"""
import importlib.util
import pathlib
import sys

_BASE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components" / "mwm_ears" / "_mwm"
)

if "mwm" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "mwm", _BASE / "__init__.py", submodule_search_locations=[str(_BASE)]
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["mwm"] = _pkg
    _spec.loader.exec_module(_pkg)
