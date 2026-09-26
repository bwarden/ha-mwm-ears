#!/usr/bin/env python3
"""Vendor the MWM protocol library from python-mwm into the integration.

PURPOSE
-------
ha-mwm-ears is a standalone Home Assistant custom integration: it ships a
private copy of the ``mwm`` protocol library under
``custom_components/mwm_ears/_mwm/`` so the component is self-contained with
no pip dependency.  The authoritative source is the published
``python-mwm`` repository, pinned here by release tag: python-mwm publishes
the tag as the contract for vendored consumers, so ``_MWM_TAG`` is the pin
and ``_MWM_REPO`` is the only place a URL belongs.

``make vendor`` (this script) shallow-clones that tag into a gitignored
cache (``.mwm/``), copies the library into ``_mwm/``, and regenerates the
effect options in ``services.yaml`` from the vendored catalogue, so the
released integration always embeds the pinned protocol logic and never
hand-re-derives the effect list.  ``make build``/``make test`` only vendor
when the cache already exists or MWM_SRC is given, so a fresh checkout --
and CI -- tests the committed copy without touching the network.

USAGE
-----
    python3 tools/vendor_mwm.py            # clone the pinned tag, then vendor
    MWM_SRC=/path/to/python-mwm/python/mwm python3 tools/vendor_mwm.py

The verbatim copies are intentionally byte-identical to source: the library
is the single source of truth, and any per-repo comment drift is dropped in
favour of clean reproduction.

Exit 0 on success; nonzero if the source can't be fetched or a file differs
in an unexpected way.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The vendored library's pin.  Bumping this is the whole "pull a new library
# release" ritual: edit the tag, run `make vendor`, commit the result.
_MWM_REPO = "https://github.com/bwarden/python-mwm"
_MWM_TAG = "v0.3.0"

# Shallow clone of _MWM_REPO at _MWM_TAG; gitignored.  The Makefile's
# MWM_CACHE default must match this path.
_CACHE = _REPO_ROOT / ".mwm" / "python-mwm"

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


def _checked_out_tag(repo: pathlib.Path) -> str:
    """The tag the cached clone is sitting on ('' if it can't be read)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "describe", "--tags", "--exact-match"],
        capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip()


def clone() -> pathlib.Path:
    """Return the library dir from a shallow clone of the pinned tag."""
    src = _CACHE / "python" / "mwm"
    if src.is_dir() and _checked_out_tag(_CACHE) == _MWM_TAG:
        return src
    # A cache left over from an older pin would silently vendor the wrong
    # library, so it is replaced rather than reused.
    shutil.rmtree(_CACHE, ignore_errors=True)
    print(f"cloning {_MWM_REPO} at {_MWM_TAG}")
    subprocess.run(
        ["git", "-c", "advice.detachedHead=false", "clone", "--quiet",
         "--depth", "1", "--branch", _MWM_TAG, _MWM_REPO, str(_CACHE)],
        check=True,
    )
    return src


def resolve_source() -> pathlib.Path:
    """The MWM_SRC override when it exists, else the published pin."""
    override = os.environ.get("MWM_SRC")
    if override:
        src = pathlib.Path(override)
        if src.is_dir():
            return src
        print(f"MWM_SRC {src} not found; using {_MWM_REPO}@{_MWM_TAG}")
    return clone()


def vendor(src: pathlib.Path) -> list[pathlib.Path]:
    """Copy the library modules into _mwm/; return the copied paths."""
    if not src.is_dir():
        sys.exit(
            f"error: source library not found at {src}\n"
            f"  pass MWM_SRC=/path/to/python-mwm/python/mwm, or make the "
            f"{_MWM_REPO} clone at {_MWM_TAG} reachable"
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
    src = resolve_source()
    copied = vendor(src)
    print(f"vendored {len(copied)} modules from {src}")
    names = effect_names()
    regenerate_services_yaml(names)
    print("vendor complete; run `make test` to verify against the vendored lib")


if __name__ == "__main__":
    main()