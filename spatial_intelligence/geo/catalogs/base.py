"""Shared catalog plumbing: HTTPS JSON fetches, STAC links, and query scoring.

Every provider module in this package builds on these helpers, so the safety
limits (HTTPS only, a 20 MiB response cap) and the keyword matching used for
natural-language searches are stated exactly once.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from ...contracts.errors import ToolInputError

#: Hard cap on one catalog JSON document.
MAX_JSON_BYTES = 20 * 1024 * 1024
#: Words that carry no signal in a natural-language catalog query.
SEARCH_STOP_WORDS = frozenset(
    {"data", "dataset", "imagery", "image", "event", "load", "open"}
)

_USER_AGENT = "spatial-intelligence/0.1 catalog-client"


def fetch_json(url: str, *, timeout: float = 30.0) -> dict:
    """Fetch one catalog JSON object over HTTPS, capped at 20 MiB.

    The transport is resolved at call time (``urllib.request.urlopen``), so a
    test or a future transport can substitute it without touching callers.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ToolInputError("catalog URLs must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_JSON_BYTES:
            raise ToolInputError(f"catalog response is too large: {length} bytes")
        raw = response.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ToolInputError("catalog response exceeded the 20 MiB safety limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ToolInputError("catalog response must be a JSON object")
    return value


def links(document: dict, rel: str) -> list[dict]:
    """Return the document's ``rel`` links that carry an href."""
    return [
        link
        for link in document.get("links", [])
        if isinstance(link, dict) and link.get("rel") == rel and link.get("href")
    ]


def event_id(url: str, link: dict) -> str:
    """Return a STAC link's event id: its title, or the parent directory name."""
    href = urljoin(url, str(link["href"]))
    fallback = Path(urlparse(href).path).parent.name or href
    return str(link.get("title") or fallback)


def search_terms(text: str) -> set[str]:
    """Normalize a title or query into comparable keyword terms.

    Plurals and gerunds are folded so "flooding" matches "flood", and the
    generic words in :data:`SEARCH_STOP_WORDS` are ignored.
    """
    raw_terms = set(re.findall(r"[a-z0-9]+", text.lower())) - SEARCH_STOP_WORDS
    terms = set()
    for term in raw_terms:
        if len(term) > 5 and term.endswith("ing"):
            term = term[:-3]
        elif len(term) > 4 and term.endswith("s"):
            term = term[:-1]
        terms.add(term)
    return terms


__all__ = [
    "MAX_JSON_BYTES",
    "SEARCH_STOP_WORDS",
    "event_id",
    "fetch_json",
    "links",
    "search_terms",
]
