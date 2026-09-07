# Packaging: Geo-AI as a single Linux AppImage (Route B)

Builds a self-contained `geo-ai-<version>-x86_64.AppImage`: Python, the full
geospatial stack (rasterio, GeoPandas, pyogrio, pyproj, shapely) and the web
UI bundled in one file. No install step, no pip, no network needed at setup —
copy it to a machine and run.

## Build

Requires Docker with BuildKit. From the repo root:

```bash
packaging/build-appimage.sh            # writes dist/geo-ai-<version>-x86_64.AppImage
```

or directly:

```bash
docker build -f packaging/Dockerfile.build --output dist .
```

The build runs inside `quay.io/pypa/manylinux_2_28_x86_64` (glibc 2.28), so
the artifact runs on any distro from roughly 2019 on (Ubuntu 20.04+,
Debian 10+, Fedora, Arch, openSUSE). Building on a newer base would inherit
its glibc and break older hosts — keep the manylinux base.

### Other architectures

For aarch64, swap three things: the base image
(`quay.io/pypa/manylinux_2_28_aarch64`), the Python path
(`/opt/python/cp312-cp312/bin` — unchanged), and the appimagetool download
(`appimagetool-aarch64.AppImage`), then build on an ARM host or with
emulation.

## Run

```bash
chmod +x geo-ai-0.0.1-x86_64.AppImage
./geo-ai-0.0.1-x86_64.AppImage
```

The server starts on `http://127.0.0.1:8000/` and opens the browser.
If the desktop lacks FUSE2 (`libfuse2`), run:

```bash
./geo-ai-0.0.1-x86_64.AppImage --appimage-extract-and-run
```

## Data and configuration

Everything the app writes lives under one data root (see `GEOAI_HOME` in
`geoai/config.py`):

| Path | Purpose |
| --- | --- |
| `~/.local/share/geo-ai/workspaces/` | workspaces (`data/`, `results/`, `maps/`, `traces/`) |
| `~/.local/share/geo-ai/settings.json` | model, theme, retries, dangerous mode |
| `~/.local/share/geo-ai/.env` | provider key and model (same schema as `.env.example`) |

Environment overrides (as in the dev checkout): `GEOAI_HOME` (data root),
`GEOAI_PORT` (default 8000), `GEOAI_NO_BROWSER=1` (headless),
`GEOAI_MODEL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`.

First run: copy `.env.example` to `~/.local/share/geo-ai/.env` and fill in
the provider key — or set the variables in the environment before launching.

## Build pipeline (Dockerfile.build, stage by stage)

1. `manylinux_2_28` + CPython 3.12, `pip install . pyinstaller`
2. `pyinstaller packaging/geo-ai.spec` — one-dir bundle at `dist/geo-ai/`.
   The spec collects everything PyInstaller cannot see statically:
   pydantic-ai's name-resolved providers/models, pydantic-ai-harness and
   geolibre data, and the geo-stack wheels' data files (proj.db, GDAL
   resources) plus uvicorn's import-string-loaded loop/protocol modules.
3. AppDir assembly (`usr/bin/geo-ai`, desktop file, icon, `.DirIcon`) and
   `appimagetool --appimage-extract-and-run` to produce the AppImage.
4. `FROM scratch AS artifacts` — the `--output` export stage.
