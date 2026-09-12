# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Geo-AI one-directory bundle (Route B).
# Built inside packaging/Dockerfile.build; entry point is the repo-root app.py.

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [(os.path.join(ROOT, "geoai", "server", "static"), "geoai/server/static")]
binaries = []
hiddenimports = [
    # uvicorn resolves loop/protocol modules by import string at runtime
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.lifespan.on",
]

# The geoai package and geoai.server load most of their modules lazily via
# __getattr__/import_module (agent, context, map_view, skills, server.app,
# server.state, ...), which PyInstaller's static analysis cannot see. Pull in
# every geoai submodule explicitly so the frozen bundle contains the whole app.
hiddenimports += collect_submodules("geoai")

# Dynamic imports and bundled data PyInstaller cannot see statically:
# pydantic-ai resolves providers/models by name (infer_model/infer_provider),
# pydantic-ai-harness and geolibre ship data files, and the geo-stack wheels
# carry data (proj.db, GDAL resources) next to their bundled shared libraries.
for pkg in (
    "pydantic_ai",
    "pydantic_ai_harness",
    "geolibre",
    "rasterio",
    "pyogrio",
    "shapely",
    "pyproj",
    "geopandas",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# packages that read their own dist metadata (importlib.metadata) at import
for dist in ("genai_prices", "pydantic_ai", "pydantic_ai_harness", "logfire", "geolibre"):
    datas += copy_metadata(dist)

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="geo-ai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="geo-ai",
)
