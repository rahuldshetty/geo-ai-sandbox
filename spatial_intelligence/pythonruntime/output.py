"""The ``run_python`` output store: one session's full output, plus its paging.

``run_python`` hands the model a bounded preview, so the whole text, the raw
stdout, and (when the last statement was an expression) the resulting object
stay here: ``inspect`` pages through the text by line, and ``query`` filters a
JSON/XML sub-value out of the structured result. The pack owns one instance per
session through :meth:`~spatial_intelligence.tools.runtime.ToolRuntime.service`,
so the agent path and the server's notebook-adjacent path share it without a
module-level global.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

#: How many lines a preview keeps.
PREVIEW_LINES = 30
#: Longest line a preview keeps (the rest is elided).
PREVIEW_LINE_CHARS = 160
#: Longest preview, in characters.
PREVIEW_TOTAL_CHARS = 6000
#: Longest rendering of a filtered result.
RENDER_MAX_CHARS = 8000

#: Sentinel for "the snippet produced no value" (``None`` is a real value).
_MISSING = object()


class OutputStore:
    """The most recent ``run_python`` output: full text, stdout, and its value.

    Attributes:
        text: The full combined output (stdout + stderr + final ``repr``).
        stdout: Raw stdout only (the structured-parse fallback).
        value: The last expression's value, or ``None`` when there is none (read
            it together with ``has_value``).
        has_value: Whether the snippet ended in an expression that evaluated.
        value_kind: ``json`` / ``xml`` / ``text`` / ``scalar`` / ``other`` /
            ``none`` — how :meth:`query` treats the value.
    """

    def __init__(self) -> None:
        self.text = ""
        self.stdout = ""
        self.value: object = None
        self.has_value = False
        self.value_kind = "none"

    # -- recording -------------------------------------------------------

    def store(
        self,
        text: str,
        value: object = _MISSING,
        has_value: bool = False,
        stdout: str = "",
    ) -> str:
        """Record an execution's output and return its truncated preview."""
        self.text = text
        self.stdout = stdout
        self.value = value if has_value else None
        self.has_value = has_value
        self.value_kind = _value_kind(value) if has_value else "none"
        return self.truncate(text)

    def truncate(self, text: str) -> str:
        """Condense ``text`` to a bounded preview for the model's context."""
        if not text:
            return text
        lines = text.splitlines()
        total = len(lines)
        out: list[str] = []
        chars = 0
        for ln in lines[:PREVIEW_LINES]:
            if len(ln) > PREVIEW_LINE_CHARS:
                ln = ln[:PREVIEW_LINE_CHARS] + "…"
            if chars + len(ln) > PREVIEW_TOTAL_CHARS:
                break
            out.append(ln)
            chars += len(ln) + 1
        if len(out) < total:
            out.append(
                f"… (output truncated: showing {len(out)} of {total} lines; "
                "use inspect_output to page, or query_output to filter JSON/XML)"
            )
        return "\n".join(out)

    # -- inspection & filtering ------------------------------------------

    def inspect(self, start: int = 0, count: int = 30) -> str:
        """Return a slice of the stored output by line index, with a header."""
        if not self.text:
            return "No run_python output is available yet."
        lines = self.text.splitlines()
        total = len(lines)
        start = max(0, int(start))
        count = max(1, min(int(count), 500))
        window = lines[start:start + count]
        return f"[{start}:{start + len(window)}] of {total} lines\n" + "\n".join(window)

    def query(self, query: str = "") -> str:
        """Filter the stored result with a jq-like path or an XPath-lite path."""
        obj, kind = self._structured_source()
        if kind == "none":
            return (
                "The last run_python result is not JSON/XML. Use inspect_output "
                "to page through its text instead."
            )
        query = (query or "").strip()
        if kind == "json":
            return _json_query(obj, query)
        return _xml_query(obj, query)

    def _structured_source(self):
        """Return ``(structured_object, kind)`` for the output, or ``(None, 'none')``."""
        if self.has_value:
            val = self.value
            kind = self.value_kind
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
        for candidate in (self.stdout.strip(), self.text.strip()):
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
    if len(text) > RENDER_MAX_CHARS:
        text = text[:RENDER_MAX_CHARS] + f"\n… (truncated at {RENDER_MAX_CHARS} chars)"
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


__all__ = ["OutputStore"]
