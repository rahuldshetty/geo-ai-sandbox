"""Python escape hatch: execute arbitrary code in the kernel process."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import threading
import traceback
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio

try:
    import osgeo  # noqa: F401  - optional (no Windows wheels for the gdal pkg)
except ImportError:  # pragma: no cover
    osgeo = None

from ..context import current

_TIMEOUT = 300.0


def run_python(code: str) -> str:
    """Run arbitrary Python in the kernel process and capture output.

    Runs with the user's kernel privileges in a fresh namespace. Prefer the
    structured tools; this is an escape hatch for math/processing no tool covers.
    Returns captured stdout, stderr, and the repr of the final expression.
    """
    ctx = current()
    ns: dict = {
        "m": ctx.map,
        "ws": ctx.workspace,
        "rasterio": rasterio,
        "osgeo": osgeo,
        "gpd": gpd,
        "np": np,
        "pd": pd,
        "json": json,
        "Path": Path,
    }

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    box: dict = {}

    def _target() -> None:
        try:
            tree = ast.parse(code)
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
