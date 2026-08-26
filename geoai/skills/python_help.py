"""Structured introspection over the ``run_python`` sandbox namespace.

The agent used to answer "what does this symbol look like?" by generating
``dir()`` / ``__doc__`` / ``inspect.signature`` snippets inside ``run_python``,
one round-trip per slice of a long docstring. ``python_help`` returns the same
information in one call: kind, signature, trimmed docstring, and public
members. API discovery becomes a single structured tool instead of a probe loop.
"""

from __future__ import annotations

import enum
import inspect
import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import rasterio
import rioxarray
import shapely
import xarray

try:
    import osgeo  # optional (no Windows wheels for the gdal pkg)
except ImportError:  # pragma: no cover
    osgeo = None

_MAX_DOC = 4000
_MAX_MEMBERS = 48
_MAX_REPR = 200

# Module roots the agent may reference inside ``run_python`` (mirrors the
# namespace bound there, plus the long-name aliases the model tends to use).
_ROOT = {
    "rasterio": rasterio,
    "rioxarray": rioxarray,
    "osgeo": osgeo,
    "numpy": np,
    "np": np,
    "pandas": pd,
    "pd": pd,
    "geopandas": gpd,
    "gpd": gpd,
    "xarray": xarray,
    "xr": xarray,
    "shapely": shapely,
    "pyproj": pyproj,
    "json": json,
}

_WS_DOC = (
    "Confined workspace facade available in run_python (no raw path/network).\n"
    "  ws.resolve(rel, must_exist=False, write=False) -> Path\n"
    "      Resolve a workspace-relative path; raises WorkspaceError outside root.\n"
    "  ws.read_text(rel, encoding='utf-8') -> str\n"
    "  ws.write_text(rel, content, encoding='utf-8') -> str  (absolute path)\n"
    "  ws.list_files(subdir='', pattern='*') -> list[str]\n"
    "  ws.data / ws.results / ws.maps -> str  (absolute directory paths)\n"
    "Prefer the structured tools (read_file/write_file) over ws for file I/O."
)


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _trim(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) > limit:
        return text[:limit].rstrip() + f"\n... [{limit} char cap]"
    return text


def _doc(obj) -> str:
    raw = getattr(obj, "__doc__", None) or ""
    return _trim(inspect.cleandoc(raw), _MAX_DOC)


def _sig(obj) -> str | None:
    try:
        return str(inspect.signature(obj))
    except (ValueError, TypeError):
        pass
    text_sig = getattr(obj, "__text_signature__", None)
    return str(text_sig) if text_sig else None


def _brief(obj) -> str:
    """One-line description of a member (for ``dir``-style listings)."""
    if inspect.ismodule(obj):
        return "module"
    if inspect.isclass(obj):
        sig = _sig(obj)
        return "class" + (sig or "()")
    if inspect.isroutine(obj):
        return _sig(obj) or "built-in (see python_help for the doc)"
    if isinstance(obj, enum.Enum):
        return f"enum member ({obj.name})"
    try:
        rep = repr(obj)
    except Exception:  # noqa: BLE001 - members can raise on repr
        rep = f"<{type(obj).__name__}>"
    return f"{type(obj).__name__} = {_trim(rep, _MAX_REPR)}"


def _members(obj) -> dict:
    out: dict[str, str] = {}
    for name in dir(obj):
        if not _is_public(name):
            continue
        try:
            out[name] = _brief(getattr(obj, name))
        except Exception:  # noqa: BLE001 - skip members that raise on access
            out[name] = "?"
        if len(out) >= _MAX_MEMBERS:
            out["..."] = f"[truncated; {_MAX_MEMBERS} of many members shown]"
            break
    return out


def _describe(name: str, obj) -> dict:
    result: dict = {"name": name, "kind": "", "doc": _doc(obj)}
    if isinstance(obj, type) and issubclass(obj, enum.Enum):
        result["kind"] = "enum"
        result["members"] = {m.name: m.value for m in obj}
    elif inspect.ismodule(obj):
        result["kind"] = "module"
        result["members"] = _members(obj)
    elif inspect.isclass(obj):
        result["kind"] = "class"
        result["signature"] = _sig(obj) or "()"
        result["members"] = _members(obj)
    elif inspect.isroutine(obj):
        result["kind"] = "function"
        sig = _sig(obj)
        if sig:
            result["signature"] = sig
    else:
        result["kind"] = "attribute"
        result["type"] = type(obj).__name__
        result["value"] = _trim(repr(obj), _MAX_REPR)
    return result


def python_help(name: str = "") -> dict:
    """Inspect a sandbox symbol's kind, signature, docstring, and members.

    Pass a dotted name reachable from the run_python namespace, e.g.
    ``"rasterio.warp.reproject"``, ``"rasterio.control.GroundControlPoint"``,
    ``"rasterio.warp.Resampling"``, ``"numpy.ndarray"``, or ``"ws"``. With no
    argument it lists the available top-level roots. Use this instead of
    writing ``dir()``/``__doc__``/``inspect.signature`` probes in run_python.
    """
    name = (name or "").strip()
    if not name:
        return {
            "kind": "namespace",
            "roots": sorted(_ROOT) + ["ws"],
            "doc": "Top-level run_python names. Query a dotted path for details.",
        }

    if name == "ws" or name.startswith("ws."):
        return {"name": name, "kind": "workspace helper", "doc": _WS_DOC}

    parts = name.split(".")
    root = parts[0]
    if root not in _ROOT:
        return {
            "name": name,
            "kind": "error",
            "doc": f"unknown root {root!r}; available: {sorted(_ROOT)}",
        }

    obj = _ROOT[root]
    try:
        for part in parts[1:]:
            obj = getattr(obj, part)
    except AttributeError as exc:
        return {"name": name, "kind": "error", "doc": f"no attribute: {exc}"}

    return _describe(name, obj)
