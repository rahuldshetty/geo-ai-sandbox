"""Python escape hatch: a confined compute sandbox in the kernel process.

``run_python`` is for math/processing the structured tools do not cover. It is
*not* a general shell: filesystem, network, subprocess, and dynamic-execution
access is rejected, and every read/write must go through the workspace-confined
``ws`` helper or the structured tools. This keeps the agent inside the workspace
even when it writes code.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import threading
import traceback

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import rasterio
import rioxarray
import shapely
import xarray

try:
    import osgeo  # noqa: F401  - optional (no Windows wheels for the gdal pkg)
except ImportError:  # pragma: no cover
    osgeo = None

from ..context import current
from ..workspace import WorkspaceError

_TIMEOUT = 300.0

# Module roots the agent may import inside ``run_python``. Everything else
# (os, sys, subprocess, pathlib, socket, importlib, urllib, requests, ctypes,
# ...) is rejected so code cannot reach outside the workspace.
_ALLOWED_IMPORTS = frozenset({
    "numpy", "pandas", "geopandas", "rasterio", "rioxarray", "xarray",
    "shapely", "pyproj",
    "json", "math", "re", "collections", "datetime", "functools", "itertools",
    "statistics", "fractions", "decimal", "copy", "random", "string", "typing",
    "enum", "contextlib", "warnings", "uuid", "textwrap", "struct",
    "dataclasses", "xml", "bisect", "heapq", "operator", "numbers",
})

# Builtins that can escape the sandbox; rejected as bare call names. Attribute
# access (e.g. ``rasterio.open``) is unaffected.
_BLOCKED_CALLS = frozenset({
    "open", "__import__", "eval", "exec", "compile", "input", "breakpoint",
})


def _guard(tree: ast.Module) -> str | None:
    """Return a violation message, or ``None`` if the code is allowed.

    This is a cooperative guard, not an OS-level security boundary: it rejects
    the obvious escape vectors so the agent stays on the structured,
    workspace-confined path. A hostile process cannot be contained by in-process
    ``exec``; that requires OS isolation.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _ALLOWED_IMPORTS:
                    return f"import of {alias.name!r} is not allowed in run_python"
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.module.split(".")[0] not in _ALLOWED_IMPORTS:
                return f"import from {node.module!r} is not allowed in run_python"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BLOCKED_CALLS:
                return f"{func.id}() is not allowed in run_python"
    return None


class _ConfinedWorkspace:
    """Path-confined facade over the active workspace (no raw ``Path``/map)."""

    def __init__(self, ws):
        self._ws = ws

    def resolve(self, rel, *, must_exist: bool = False, write: bool = False):
        return self._ws.resolve(rel, must_exist=must_exist, write=write)

    def read_text(self, rel: str, encoding: str = "utf-8") -> str:
        return self._ws.resolve(rel, must_exist=True).read_text(
            encoding=encoding, errors="replace"
        )

    def write_text(self, rel: str, content: str, encoding: str = "utf-8") -> str:
        out = self._ws.resolve(rel, write=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding=encoding)
        rel_out = out.relative_to(self._ws.root).as_posix()
        self._ws.record_output(rel_out)
        return str(out)

    def list_files(self, subdir: str = "", pattern: str = "*") -> list[str]:
        return self._ws.list_files(subdir, pattern)

    @property
    def data(self) -> str:
        return str(self._ws.data)

    @property
    def results(self) -> str:
        return str(self._ws.results)

    @property
    def maps(self) -> str:
        return str(self._ws.maps)


def run_python(code: str) -> str:
    """Run a snippet of Python for math/processing and capture its output.

    The sandbox exposes the geospatial stack (``numpy``, ``pandas``, ``geopandas``,
    ``rasterio``, ``rioxarray``, ``xarray``, ``shapely``, ``pyproj``) and a
    workspace-confined ``ws`` helper. File I/O, network, subprocess, and dynamic
    execution are rejected — use the structured tools (``read_file``,
    ``write_file``, the raster/vector tools) for any filesystem access and the
    map tools for the live map. Returns captured stdout, stderr, and the repr of
    the final expression.
    """
    ctx = current()
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"

    violation = _guard(tree)
    if violation is not None:
        return f"run_python blocked: {violation}. Use the structured tools instead."

    ns: dict = {
        "ws": _ConfinedWorkspace(ctx.workspace),
        "rasterio": rasterio,
        "rioxarray": rioxarray,
        "osgeo": osgeo,
        "gpd": gpd,
        "np": np,
        "pd": pd,
        "xr": xarray,
        "shapely": shapely,
        "pyproj": pyproj,
        "json": json,
    }

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    box: dict = {}

    def _target() -> None:
        try:
            body = tree.body
            last_expr = None
            if body and isinstance(body[-1], ast.Expr):
                last_expr = ast.Expression(body.pop().value)
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                if body:
                    module = ast.fix_missing_locations(
                        ast.Module(body=body, type_ignores=[])
                    )
                    exec(compile(module, "<run_python>", "exec"), ns)  # noqa: S102
                if last_expr is not None:
                    expr = ast.fix_missing_locations(last_expr)
                    box["value"] = eval(compile(expr, "<run_python>", "eval"), ns)  # noqa: S307
                    box["has"] = True
        except Exception:
            err_buf.write(traceback.format_exc())

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(_TIMEOUT)
    if thread.is_alive():
        return f"run_python timed out after {int(_TIMEOUT)}s"

    parts: list[str] = []
    if out_buf.getvalue():
        parts.append(out_buf.getvalue().rstrip())
    if err_buf.getvalue():
        parts.append(err_buf.getvalue().rstrip())
    if box.get("has"):
        parts.append("=> " + repr(box["value"]))
    return "\n".join(parts)
