# Packaging: Spatial Intelligence as a single Linux AppImage (Route B)

Builds a self-contained `spatial-intelligence-<version>-x86_64.AppImage`: Python,
the full geospatial stack (rasterio, GeoPandas, pyogrio, pyproj, shapely), the
web front end, and the `run_python` sandbox bundled in one file. No install
step, no pip, no network needed at setup — copy it to a machine and run.

## Build

Requires Docker with BuildKit. From the repo root:

```bash
packaging/build-appimage.sh            # writes dist/spatial-intelligence-<version>-x86_64.AppImage
```

or directly:

```bash
docker build -f packaging/Dockerfile.build --output dist .
```

The build runs inside `quay.io/pypa/manylinux_2_28_x86_64` (glibc 2.28), so
the artifact runs on any distro from roughly 2019 on (Ubuntu 20.04+,
Debian 10+, Fedora, Arch, openSUSE). Building on a newer base would inherit
its glibc and break older hosts — keep the manylinux base.

Two things the image does that the dev checkout does not:

- It downloads the pinned `marked` Markdown bundle into
  `spatial_intelligence/web/vendor/` before PyInstaller runs. The bundle
  directory inside a mounted AppImage is read-only, so the app's first-launch
  bootstrap cannot write there — the asset has to ship inside the image.
  `spatial_intelligence/web/vendor` is excluded from the build context so a
  copy on the build host cannot decide what ships.
- It pins CPython 3.12 (`uv venv --python 3.12`) rather than taking uv's
  default, so the bundled interpreter is the one the project is developed on.

### Other architectures

For aarch64, swap three things: the base image
(`quay.io/pypa/manylinux_2_28_aarch64`), the Python path
(`/opt/python/cp312-cp312/bin` — unchanged), and the appimagetool download
(`appimagetool-aarch64.AppImage`), then build on an ARM host or with
emulation.

## Run

```bash
chmod +x spatial-intelligence-0.0.1-x86_64.AppImage
./spatial-intelligence-0.0.1-x86_64.AppImage
```

The server starts on `http://127.0.0.1:8000/` and opens the browser.
If the desktop lacks FUSE2 (`libfuse2`), run:

```bash
./spatial-intelligence-0.0.1-x86_64.AppImage --appimage-extract-and-run
```

## Data and configuration

Everything the app writes lives under one data root (see `GEOAI_HOME` in
`spatial_intelligence/settings/env.py`):

| Path | Purpose |
| --- | --- |
| `~/.local/share/geo-ai/workspaces/` | workspaces (`data/`, `results/`, `maps/`, `traces/`) |
| `~/.local/share/geo-ai/settings.json` | model, theme, retries, dangerous mode |
| `~/.local/share/geo-ai/.env` | provider key and model (same schema as `.env.example`) |

The directory name is unchanged from the previous package on purpose: an
existing installation keeps its workspaces and settings. Only a checkout (a
`.git` or `workspaces/` next to the package) writes next to the sources, which
a frozen bundle never is.

Environment overrides (as in the dev checkout): `GEOAI_HOME` (data root),
`GEOAI_PORT` (default 8000), `GEOAI_NO_BROWSER=1` (headless),
`GEOAI_MODEL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`.

First run: copy `.env.example` to `~/.local/share/geo-ai/.env` and fill in
the provider key — or set the variables in the environment before launching.
Until a key is configured, opening/creating a workspace fails with a
provider error ("Set the `OPENAI_API_KEY` environment variable..."); the
rest of the app (map, python cells, file data) works without one.

## Build pipeline (Dockerfile.build, stage by stage)

1. `manylinux_2_28` + CPython 3.12, `uv pip install . pyinstaller`
2. the pinned Marked bundle, fetched into the source tree
3. `pyinstaller packaging/spatial-intelligence.spec` — one-dir bundle at
   `dist/spatial-intelligence/`. The spec collects everything PyInstaller
   cannot see statically: the `spatial_intelligence` submodules reached only
   through import strings, the `web/` assets, pydantic-ai's name-resolved
   providers/models, pydantic-ai-harness and geolibre data, and the geo-stack
   wheels' data files (proj.db, GDAL resources) plus uvicorn's
   import-string-loaded loop/protocol modules.
4. AppDir assembly (`usr/bin/spatial-intelligence`, desktop file, icon,
   `.DirIcon`) and `appimagetool --appimage-extract-and-run` to produce the
   AppImage.
5. `FROM scratch AS artifacts` — the `--output` export stage.
