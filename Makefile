# Geo-AI development & release targets.

VENV    := .venv
PYTHON  ?= python3
PIP     := $(VENV)/bin/pip
PY      := $(VENV)/bin/python
DIST    := dist

.PHONY: help venv dev run release appimage clean

help:
	@echo "Targets:"
	@echo "  make venv     create $(VENV) and install the package (editable)"
	@echo "  make dev      create venv if needed, then run the dev server"
	@echo "  make run      alias for make dev"
	@echo "  make release  build the AppImage into $(DIST)/ (requires docker)"
	@echo "  make appimage alias for make release"
	@echo "  make clean    remove $(VENV) and build artifacts"

venv:
	@if [ ! -x $(PY) ]; then \
		echo "Creating virtualenv with $(PYTHON)"; \
		$(PYTHON) -m venv $(VENV); \
	fi
	$(PIP) install --upgrade pip
	$(PIP) install -e .

dev: venv
	$(PY) -m geoai.server

run: dev

release:
	./packaging/build-appimage.sh $(DIST)

appimage: release

clean:
	rm -rf $(VENV) $(DIST) build build-pyi