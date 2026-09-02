# Standalone build for the mwm_ears Home Assistant custom integration.
#
# The component is self-contained: the vendored _mwm protocol library and
# the front-end card ship inside custom_components/. There are no external
# test dependencies here -- the unit suite is pure stdlib, so you can run it
# on the HA host or any machine with Python 3 (homeassistant is not required;
# the platform modules get a compile-all smoke check only).

PYTHON ?= python3

.PHONY: build test dist clean

# Syntax-check every module in the component (incl. the HA platform files,
# which aren't importable here but must still compile).
build:
	PYTHONPATH=custom_components $(PYTHON) -m compileall -q custom_components

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