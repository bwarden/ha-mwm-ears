# Standalone build for the mwm_ears Home Assistant custom integration.
#
# The component is self-contained: the vendored _mwm protocol library and
# the front-end card ship inside custom_components/. There are no external
# test dependencies here -- the unit suite is pure stdlib, so you can run it
# on the HA host or any machine with Python 3 (homeassistant is not required;
# the platform modules get a compile-all smoke check only).

PYTHON ?= python3

# The MWM protocol library is vendored from the published python-mwm repo,
# pinned to the release tag recorded in tools/vendor_mwm.py (_MWM_TAG).
# `make vendor` shallow-clones that tag into MWM_CACHE (gitignored) and
# re-vendors on every build afterwards; a fresh checkout -- and CI -- has no
# cache, so it tests the committed _mwm/ copy without touching the network.
# Override with MWM_SRC=/path/to/python-mwm/python/mwm to vendor from a
# local checkout instead.
MWM_CACHE ?= .mwm/python-mwm
MWM_SRC ?=

.PHONY: build test dist vendor clean

# Re-vendor the MWM protocol library from the pinned tag (when a source is
# available) and regenerate services.yaml from it, then syntax-check every
# module in the component (incl. the HA platform files, which aren't
# importable here but must still compile).
build:
	@if [ -n "$(MWM_SRC)" ] || [ -d "$(MWM_CACHE)" ]; then \
		echo "vendoring MWM library"; \
		MWM_SRC="$(MWM_SRC)" $(PYTHON) tools/vendor_mwm.py; \
	else \
		echo "no MWM source (run 'make vendor' to pull $(MWM_CACHE)); using committed _mwm"; \
	fi
	PYTHONPATH=custom_components $(PYTHON) -m compileall -q custom_components

# Pull the pinned MWM protocol library release into _mwm/ (cloning the
# published python-mwm tag if needed) and regenerate services.yaml from it.
vendor:
	MWM_SRC="$(MWM_SRC)" $(PYTHON) tools/vendor_mwm.py

# The vendored protocol library and pair-state logic are unit-tested from
# tests/; the platform (HA-coupled) modules get their compileall above.
test: build
	PYTHONPATH=. $(PYTHON) -m unittest discover -s tests

# The HA distribution: a zip of custom_components/ (minus __pycache__),
# ready to unpack into <config>/custom_components/.
dist: build
	rm -rf dist
	mkdir -p dist
	zip -qr dist/mwm_ears.zip custom_components -x '*__pycache__*' '*/__pycache__/*'
	@echo "Home Assistant distribution ready in dist/mwm_ears.zip"

clean:
	rm -rf dist custom_components/**/__pycache__