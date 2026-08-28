"""Python escape hatch: a compute sandbox in the kernel process.

``run_python`` is for math/processing the structured tools do not cover. In
safe mode (the default) it allows the geospatial stack plus basic stdlib
(``os``, ``sys``, ``pathlib``, ``shutil``, ...) but rejects subprocess, network,
dynamic execution, and raw command calls so code stays inside the workspace.
With dangerous mode enabled every guard is lifted and the snippet runs as an
arbitrary Python program.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import threading
import traceback
import xml.etree.ElementTree as ET

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

# Dangerous mode lifts every sandbox guard so ``run_python`` can run arbitrary
# code (subprocess, network, filesystem outside the workspace, dynamic exec).
# It is off by default and toggled from the UI; the server keeps this flag in
# sync with the persisted setting before each run.
_dangerous_mode = False


def set_dangerous_mode(enabled: bool) -> None:
    """Enable/disable the arbitrary-command escape hatch."""
    global _dangerous_mode
    _dangerous_mode = bool(enabled)


# Module roots the agent may import inside ``run_python`` in safe mode: the
# geospatial stack plus basic stdlib (``os``, ``sys``, ``pathlib``, ``shutil``,
# ...). Dangerous escape vectors (subprocess, socket, importlib, urllib,
# requests, ctypes, ...) stay rejected.
_ALLOWED_IMPORTS = frozenset({
    "numpy", "pandas", "geopandas", "rasterio", "rioxarray", "xarray",
    "shapely", "pyproj",
    "os", "sys", "pathlib", "shutil", "time", "glob", "csv", "tempfile",
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

# Dotted attribute calls that spawn processes or run commands; rejected in safe
# mode even though ``os`` is importable. Dangerous mode lifts this.
_BLOCKED_ATTR_CALLS = frozenset({
    "os.system", "os.popen", "os.popen2", "os.popen3", "os.popen4",
    "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
    "os.execv", "os.execl", "os.execve", "os.execle", "os.execvp",
    "os.execvpe", "os.execlp", "os.execlpe", "os.fork", "os.forkpty",
    "os.startfile",
})


def _dotted_name(node: ast.AST) -> str | None:
    """Reconstruct an attribute chain (e.g. ``os.system``) from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is not None:
            return f"{base}.{node.attr}"
    return None


def _guard(tree: ast.Module) -> str | None:
    """Return a violation message, or ``None`` if the code is allowed in safe mode.

    This is a cooperative guard, not an OS-level security boundary: it rejects
    the obvious escape vectors (subprocess, network, dynamic execution, raw
    command calls) so the agent stays on the structured, workspace-confined
    path. Dangerous mode bypasses this entirely. A hostile process cannot be
    contained by in-process ``exec``; that requires OS isolation.
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
            dotted = _dotted_name(func)
            if dotted in _BLOCKED_ATTR_CALLS:
                return f"{dotted}() is not allowed in run_python (use dangerous mode)"
    return None


# -- output store & preview ------------------------------------------------

# Bounds on what ``run_python`` returns to the model: the full text is kept in
# ``_last_output`` and paged through with ``inspect_output`` / ``query_output``.
_PREVIEW_LINES = 30
_PREVIEW_LINE_CHARS = 160
_PREVIEW_TOTAL_CHARS = 6000
_RENDER_MAX_CHARS = 8000

_MISSING = object()

_last_output: dict = {
    "text": "",       # full combined output (stdout + stderr + final repr)
    "stdout": "",     # raw stdout only (structured-parse fallback)
    "value": _MISSING,
    "has_value": False,
    "value_kind": "none",
}


def _truncate(text: str) -> str:
    """Condense ``text`` to a bounded preview for the model's context."""
    if not text:
        return text
    lines = text.splitlines()
    total = len(lines)
    out: list[str] = []
    chars = 0
    for ln in lines[:_PREVIEW_LINES]:
        if len(ln) > _PREVIEW_LINE_CHARS:
            ln = ln[:_PREVIEW_LINE_CHARS] + "…"
        if chars + len(ln) > _PREVIEW_TOTAL_CHARS:
            break
        out.append(ln)
        chars += len(ln) + 1
    if len(out) < total:
        out.append(
            f"… (output truncated: showing {len(out)} of {total} lines; "
            "use inspect_output to page, or query_output to filter JSON/XML)"
        )
    return "\n".join(out)


def _value_kind(value) -> str:
    """Classify a final-expression value for structured filtering."""
    if isinstance(value, (dict, list, tuple)):
        return "json"
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") or s.startswith("["):
            return "json"
        if s.startswith("<") and s.endswith(">"):
            return "xml"
        return "text"
    if ET.iselement(value) or isinstance(value, ET.ElementTree):
        return "xml"
    if isinstance(value, (int, float, bool)) or value is None:
        return "scalar"
    return "other"


def _finish(text: str, value=_MISSING, has_value: bool = False, stdout: str = "") -> str:
    """Store the latest output and return its truncated preview."""
    _last_output.update({
        "text": text,
        "stdout": stdout,
        "value": value,
        "has_value": has_value,
        "value_kind": _value_kind(value) if has_value else "none",
    })
    return _truncate(text)



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

    In safe mode the sandbox exposes the geospatial stack (``numpy``, ``pandas``,
    ``geopandas``, ``rasterio``, ``rioxarray``, ``xarray``, ``shapely``, ``pyproj``),
    basic stdlib (``os``, ``sys``, ``pathlib``, ``shutil``, ...), and a
    workspace-confined ``ws`` helper; subprocess, network, dynamic execution,
    and raw command calls are rejected. With dangerous mode enabled every guard
    is lifted and the snippet runs as arbitrary Python.

    The returned text is a bounded preview (first few lines). The full output is
    stored and can be paged with ``inspect_output`` or filtered with
    ``query_output`` when the result is JSON/XML.
    """
    ctx = current()
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return _finish(f"SyntaxError: {exc}")

    if not _dangerous_mode:
        violation = _guard(tree)
        if violation is not None:
            return _finish(
                f"run_python blocked: {violation}. Use the structured tools, or "
                "enable dangerous mode."
            )

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
        return _finish(f"run_python timed out after {int(_TIMEOUT)}s")

    stdout = out_buf.getvalue()
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip())
    if err_buf.getvalue():
        parts.append(err_buf.getvalue().rstrip())
    has_value = box.get("has", False)
    if has_value:
        parts.append("=> " + repr(box["value"]))
    return _finish(
        "\n".join(parts),
        value=box.get("value", _MISSING),
        has_value=has_value,
        stdout=stdout,
    )


# -- output inspection & filtering -----------------------------------------


def get_last_output_text() -> str:
    """Return the full text of the most recent ``run_python`` execution."""
    return _last_output["text"]


def inspect_output(start: int = 0, count: int = 30) -> str:
    """Read a slice of the last ``run_python`` output by line index.

    ``start`` is 0-based and ``count`` is the maximum number of lines to return
    (capped at 500). Use this to page through a large output that ``run_python``
    truncated. Returns a header with the line range and total, then the lines.
    """
    text = _last_output["text"]
    if not text:
        return "No run_python output is available yet."
    lines = text.splitlines()
    total = len(lines)
    start = max(0, int(start))
    count = max(1, min(int(count), 500))
    window = lines[start:start + count]
    return f"[{start}:{start + len(window)}] of {total} lines\n" + "\n".join(window)


def query_output(query: str = "") -> str:
    """Filter the last ``run_python`` result with a jq-like path or XPath-lite.

    JSON results support ``.`` (whole), ``.key.subkey``, ``[index]``,
    ``[start:end]``, and the bare filters ``keys`` and ``length``. XML results
    support tag paths relative to the root (``child``, ``child/grandchild``),
    descendant search (``.//tag``), a positional ``[n]``, plus ``/@attr`` and
    ``/text()``. With an empty query, returns a schema summary of the last
    structured result.
    """
    obj, kind = _structured_source()
    if kind == "none":
        return (
            "The last run_python result is not JSON/XML. Use inspect_output "
            "to page through its text instead."
        )
    query = (query or "").strip()
    if kind == "json":
        return _json_query(obj, query)
    return _xml_query(obj, query)


def _structured_source():
    """Return ``(structured_object, kind)`` for the last output, or ``(None, 'none')``."""
    out = _last_output
    if out["has_value"]:
        val = out["value"]
        kind = out["value_kind"]
        if kind == "json":
            if isinstance(val, str):
                try:
                    return json.loads(val), "json"
                except (ValueError, TypeError):
                    pass
            else:
                return val, "json"
        elif kind == "xml":
            try:
                return _xml_root(val), "xml"
            except (ValueError, ET.ParseError):
                pass
    for candidate in (out["stdout"].strip(), out["text"].strip()):
        if not candidate:
            continue
        try:
            return json.loads(candidate), "json"
        except (ValueError, TypeError):
            pass
        try:
            return ET.fromstring(candidate), "xml"
        except ET.ParseError:
            pass
    return None, "none"


def _xml_root(value) -> ET.Element:
    if isinstance(value, ET.ElementTree):
        return value.getroot()
    if isinstance(value, ET.Element):
        return value
    if isinstance(value, str):
        return ET.fromstring(value)
    raise ValueError(f"not an XML value: {type(value).__name__}")


def _path_tokens(path: str) -> list[str]:
    """Split a jq-like path into key / ``[i]`` / ``[a:b]`` / word tokens."""
    tokens: list[str] = []
    i, n = 0, len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            i += 1
            start = i
            while i < n and (path[i].isalnum() or path[i] in "_-"):
                i += 1
            if i > start:
                tokens.append(path[start:i])
        elif ch == "[":
            end = path.find("]", i)
            if end == -1:
                tokens.append(path[i:])
                break
            tokens.append(path[i:end + 1])
            i = end + 1
        else:
            start = i
            while i < n and (path[i].isalnum() or path[i] in "_-"):
                i += 1
            if i > start:
                tokens.append(path[start:i])
            else:
                i += 1
    return tokens


def _keys_hint(d: dict) -> str:
    keys = list(d.keys())[:20]
    return ", ".join(repr(k) for k in keys)


def _eval_path(obj, path: str):
    """Evaluate a jq-like path over a Python object (dict/list/primitive)."""
    cur = obj
    for tok in _path_tokens(path):
        if tok == ".":
            continue
        if tok == "keys":
            if isinstance(cur, dict):
                cur = list(cur.keys())
            elif isinstance(cur, list):
                cur = list(range(len(cur)))
            else:
                raise ValueError(f"'keys' needs a dict/list, got {type(cur).__name__}")
        elif tok == "length":
            try:
                cur = len(cur)
            except TypeError:
                raise ValueError("'length' is not available on this value") from None
        elif tok.startswith("["):
            inner = tok[1:-1].strip()
            if not isinstance(cur, (list, tuple, str)):
                raise ValueError(f"{tok!r} needs a list/string, got {type(cur).__name__}")
            if ":" in inner:
                a, _, b = inner.partition(":")
                start = int(a) if a.strip() else None
                end = int(b) if b.strip() else None
                cur = cur[slice(start, end)]
            else:
                cur = cur[int(inner)]
        else:
            if isinstance(cur, dict):
                if tok not in cur:
                    raise ValueError(f"no key {tok!r}; available: {_keys_hint(cur)}")
                cur = cur[tok]
            else:
                raise ValueError(f"{tok!r} needs a dict, got {type(cur).__name__}")
    return cur


def _render(value) -> str:
    """Render a filtered result compactly, bounded for the model's context."""
    if isinstance(value, str):
        text = repr(value)
    elif isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    else:
        text = repr(value)
    if len(text) > _RENDER_MAX_CHARS:
        text = text[:_RENDER_MAX_CHARS] + f"\n… (truncated at {_RENDER_MAX_CHARS} chars)"
    return text


def _summarize(obj) -> str:
    """Produce a compact schema summary for an empty query."""
    if isinstance(obj, dict):
        keys = list(obj.keys())
        shown = keys[:40]
        lines = [f"dict with {len(keys)} keys: {shown}"]
        if len(keys) > len(shown):
            lines.append(f"… ({len(keys) - len(shown)} more keys)")
        if shown:
            lines.append(f"first value {shown[0]!r}: {_render(obj[shown[0]])[:300]}")
        return "\n".join(lines)
    if isinstance(obj, (list, tuple)):
        lines = [f"list with {len(obj)} items"]
        if obj:
            lines.append(f"first item: {_render(obj[0])[:300]}")
        return "\n".join(lines)
    return _render(obj)


def _json_query(obj, query: str) -> str:
    if query in ("", "."):
        return _summarize(obj)
    try:
        return _render(_eval_path(obj, query))
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        return f"query failed: {exc}"


def _xml_text(elem: ET.Element) -> str:
    return elem.text or ""


def _xml_compact(elem: ET.Element) -> dict:
    return {
        "tag": elem.tag,
        "attrs": dict(elem.attrib),
        "text": (elem.text or "").strip()[:200],
        "children": list(dict.fromkeys(c.tag for c in elem))[:20],
        "child_count": len(elem),
    }


def _summarize_xml(root: ET.Element) -> str:
    children = list(dict.fromkeys(c.tag for c in root))[:20]
    return (
        f"XML root <{root.tag}> with {len(root)} child elements; "
        f"attrs {dict(root.attrib)}; child tags: {children}"
    )


def _xml_query(root: ET.Element, query: str) -> str:
    q = query.strip()
    if q in ("", "."):
        return _summarize_xml(root)
    attr = None
    want_text = False
    if q.endswith("/text()"):
        want_text = True
        q = q[: -len("/text()")].rstrip("/") or "."
    elif "/@" in q:
        q, _, attr = q.rpartition("/@")
        q = q or "."
    multiple = q.startswith(".//") or q.startswith("//") or "*" in q
    try:
        if multiple:
            elems = root.findall(q)
            if not elems:
                return f"no XML matches for {q!r}"
            if attr is not None:
                return "\n".join(str(e.get(attr)) for e in elems)
            if want_text:
                return "\n".join(_xml_text(e) for e in elems)
            return _render([_xml_compact(e) for e in elems])
        elem = root.find(q)
        if elem is None:
            return f"no XML match for {q!r}"
        if attr is not None:
            return str(elem.get(attr))
        if want_text:
            return _xml_text(elem)
        return _render(_xml_compact(elem))
    except SyntaxError as exc:
        return f"invalid XML path: {exc}"
