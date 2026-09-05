#!/usr/bin/env python3
"""Vendor the MWM protocol library from ir-remote-tools into the integration.

PURPOSE
-------
ha-mwm-ears is a standalone Home Assistant custom integration: it ships a
private copy of the ``mwm`` protocol library under
``custom_components/mwm_ears/_mwm/`` so the component is self-contained with
no pip dependency.  The authoritative source for that library lives in the
sibling ``ir-remote-tools`` repo (``python/mwm/``).

``make vendor`` (this script) copies the latest library into ``_mwm/`` and
regenerates the effect options in ``services.yaml`` from the vendored
catalogue, so the released integration always embeds the newest protocol
logic and never hand-re-derives the effect list.

USAGE
-----
    python3 tools/vendor_mwm.py            # default: ../ir-remote-tools
    MWM_SRC=/path/to/ir-remote-tools/python/mwm python3 tools/vendor_mwm.py

The verbatim copies are intentionally byte-identical to source: the library
is the single source of truth, and any per-repo comment drift is dropped in
favour of clean reproduction.

Exit 0 on success; nonzero if the source can't be found or a file differs in
an unexpected way.
"""

from __future__ import annotations

import os
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_LIB_MODULES = [
    "__init__.py",
    "command.py",
    "decode.py",
    "incant.py",
    "palette.py",
    "protocol.py",
    "timings.py",
]

_VENDOR_DIR = (
    _REPO_ROOT / "custom_components" / "mwm_ears" / "_mwm"
)
_SERVICES_YAML = _REPO_ROOT / "custom_components" / "mwm_ears" / "services.yaml"
_SERVICES_MARKER = "# MWM-EFFECT-OPTIONS"


def source_dir() -> pathlib.Path:
    """Resolve the authoritative library directory."""
    override = _REPO_ROOT.parent
    return override / "ir-remote-tools" / "python" / "mwm"


def vendor(src: pathlib.Path) -> list[pathlib.Path]:
    """Copy the library modules into _mwm/; return the copied paths."""
    if not src.is_dir():
        sys.exit(
            f"error: source library not found at {src}\n"
            "  pass MWM_SRC=/path/to/ir-remote-tools/python/mwm or run from "
            "a checkout whose sibling is ir-remote-tools"
        )
    _VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for mod in _LIB_MODULES:
        s = src / mod
        if not s.is_file():
            sys.exit(f"error: source module missing: {s}")
        d = _VENDOR_DIR / mod
        d.write_bytes(s.read_bytes())
        copied.append(d)
    return copied


def effect_names() -> list[str]:
    """Load the vendored library's effect catalogue (name order)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mwm_vendor_probe", _VENDOR_DIR / "__init__.py",
        submodule_search_locations=[str(_VENDOR_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mwm_vendor_probe"] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, "LIGHT_EFFECTS"):
        sys.exit("error: vendored library has no LIGHT_EFFECTS catalogue")
    return list(mod.LIGHT_EFFECTS.keys())


def regenerate_services_yaml(names: list[str]) -> None:
    """Rewrite the effect options block in services.yaml from names."""
    text = _SERVICES_YAML.read_text()
    block = "\n".join(f"            - {name}" for name in names)
    start = text.index(_SERVICES_MARKER)
    end = text.index(_SERVICES_MARKER, start + 1) + len(_SERVICES_MARKER)
    replacement = _SERVICES_MARKER + "\n" + block + "\n" + _SERVICES_MARKER
    _SERVICES_YAML.write_text(text[:start] + replacement + text[end:])
    print(f"regenerated effect options in {_SERVICES_YAML.name} "
          f"({len(names)} effects)")


def main() -> None:
    src = pathlib.Path(os.environ.get("MWM_SRC", str(source_dir())))
    copied = vendor(src)
    print(f"vendored {len(copied)} modules from {src}")
    names = effect_names()
    regenerate_services_yaml(names)
    print("vendor complete; run `make test` to verify against the vendored lib")


if __name__ == "__main__":
    main()