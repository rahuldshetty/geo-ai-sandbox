# Spatial Intelligence — development and release targets.

VENV   := .venv
DIST   := dist
PYTHON ?= python3
UV     ?= uv

ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
PY  := $(BIN)/python.exe
else
BIN := $(VENV)/bin
PY  := $(BIN)/python
endif

# The repo root must be importable even before `make venv` has installed the
# package, and it should win over an installed copy while editing.
export PYTHONPATH := .

.PHONY: help venv dev run test release appimage clean

help:
	@echo "Targets:"
	@echo "  make venv         create $(VENV) if missing, then install the package (editable)"
	@echo "  make dev          run the app (python -m spatial_intelligence.server)"
	@echo "  make run          alias for make dev"
	@echo "  make test         run the test suite"
	@echo "  make release      build the AppImage into $(DIST)/ (requires docker)"
	@echo "  make appimage     alias for make release"
	@echo "  make clean        remove $(VENV), caches, and build artifacts"

# Installs prefer uv: this checkout's .venv is uv-managed and contains no pip.
# A plain `python -m venv` falls back to pip.
venv:
	@if [ ! -x "$(PY)" ]; then \
		if command -v $(UV) >/dev/null 2>&1; then \
			echo "Creating virtualenv with uv"; \
			$(UV) venv $(VENV); \
		else \
			echo "Creating virtualenv with $(PYTHON)"; \
			$(PYTHON) -m venv $(VENV); \
		fi; \
	fi
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) pip install -e .; \
	else \
		"$(PY)" -m pip install --upgrade pip && "$(PY)" -m pip install -e .; \
	fi

# The interpreter doubles as the "environment is ready" marker, so `make dev`
# sets up once and never reinstalls on later runs.
$(PY):
	@$(MAKE) --no-print-directory venv

dev: $(PY)
	"$(PY)" -m spatial_intelligence.server

run: dev

# unittest discovery needs the repo root on the path and `tests/` importable as
# a package; `export PYTHONPATH := .` above supplies the former.
test: $(PY)
	"$(PY)" -m unittest discover -s tests -t . -p "test_*.py"

# Linux AppImage of this app (see packaging/README.md). Requires docker.
release:
	./packaging/build-appimage.sh $(DIST)

appimage: release

clean:
	rm -rf $(VENV) $(DIST) build build-pyi
	-find spatial_intelligence tests -name __pycache__ -type d -prune -exec rm -rf {} +
