# Standalone build for the mwm_ears Home Assistant custom integration.
#
# The component is self-contained: the vendored _mwm protocol library and
# the front-end card ship inside custom_components/. There are no external
# test dependencies here -- the unit suite is pure stdlib, so you can run it
# on the HA host or any machine with Python 3 (homeassistant is not required;
# the platform modules get a compile-all smoke check only).

PYTHON ?= python3

# The MWM protocol library is re-vendored from the sibling python-mwm repo
# on every build, so tests and dist always exercise the latest library.
# Override with MWM_SRC=/path/to/python-mwm/python/mwm.  In a checkout
# without the sibling (CI), the committed _mwm/ copy is used as-is.
MWM_SRC ?= ../python-mwm/python/mwm

.PHONY: build test dist vendor clean

# Pull the latest MWM protocol library from the sibling python-mwm repo
# into _mwm/ (when available) and regenerate services.yaml from it, then
# syntax-check every module in the component (incl. the HA platform files,
# which aren't importable here but must still compile).
build:
	@if [ -d "$(MWM_SRC)" ]; then \
		echo "vendoring MWM library from $(MWM_SRC)"; \
		MWM_SRC="$(MWM_SRC)" $(PYTHON) tools/vendor_mwm.py; \
	else \
		echo "MWM_SRC $(MWM_SRC) not present; using committed _mwm"; \
	fi
	PYTHONPATH=custom_components $(PYTHON) -m compileall -q custom_components

# Re-vendor the MWM library + regenerate services.yaml (build already does
# this; the target exists for explicit/cron use and for CI-style checkouts
# that lack the sibling).  Override the source with
# MWM_SRC=/path/to/python-mwm/python/mwm.
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