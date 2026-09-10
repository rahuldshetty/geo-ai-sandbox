"""Open imagery catalogs using the same public contracts as GeoLibre 2.9."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from ..context import current
from .map_tools import _persist, _require_map
from .workspace_tools import download

_VANTOR_CATALOG = "https://vantor-opendata.s3.amazonaws.com/events/catalog.json"
_OAM_API = "https://api.openaerialmap.org"
_OAM_TILER = "https://titiler.hotosm.org"
_MAX_JSON_BYTES = 20 * 1024 * 1024
_cache_lock = threading.Lock()
_scene_cache: dict[tuple[str, str], dict[str, Any]] = {}
_SEARCH_STOP_WORDS = {"data", "dataset", "imagery", "image", "event", "load", "open"}


def _fetch_json(url: str, *, timeout: float = 30.0) -> dict:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("catalog URLs must use HTTPS")
    req = Request(url, headers={"User-Agent": "GeoAI/0.1 catalog-client"})
    with urlopen(req, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > _MAX_JSON_BYTES:
            raise ValueError(f"catalog response is too large: {length} bytes")
        raw = response.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("catalog response exceeded the 20 MiB safety limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("catalog response must be a JSON object")
    return value


def _links(document: dict, rel: str) -> list[dict]:
    return [
        link
        for link in document.get("links", [])
        if isinstance(link, dict) and link.get("rel") == rel and link.get("href")
    ]


def _event_id(url: str, link: dict) -> str:
    href = urljoin(url, str(link["href"]))
    fallback = Path(urlparse(href).path).parent.name or href
    return str(link.get("title") or fallback)


def _search_terms(text: str) -> set[str]:
    raw_terms = set(re.findall(r"[a-z0-9]+", text.lower())) - _SEARCH_STOP_WORDS
    terms = set()
    for term in raw_terms:
        if len(term) > 5 and term.endswith("ing"):
            term = term[:-3]
        elif len(term) > 4 and term.endswith("s"):
            term = term[:-1]
        terms.add(term)
    return terms


def search_vantor_events(query: str = "", limit: int = 20) -> list[dict]:
    """Search Vantor Open Data disaster events by title.

    This uses the Vantor STAC catalog consumed by GeoLibre's Vantor Open Data
    plugin. Call ``search_vantor_imagery`` with one returned event id.
    """
    catalog = _fetch_json(_VANTOR_CATALOG)
    query_terms = _search_terms(query)
    events: list[tuple[int, dict]] = []
    for link in _links(catalog, "child"):
        event_id = _event_id(_VANTOR_CATALOG, link)
        title = str(link.get("title") or event_id)
        score = len(query_terms & _search_terms(title))
        if query_terms and score < len(query_terms):
            continue
        events.append(
            (
                score,
                {
                    "id": event_id,
                    "title": title,
                    "provider": "Vantor Open Data",
                },
            )
        )
    events.sort(key=lambda item: (-item[0], item[1]["title"]))
    return [event for _, event in events[: max(1, min(limit, 100))]]


def _bbox_intersects(item_bbox: Any, bounds: list[float] | None) -> bool:
    if bounds is None or not isinstance(item_bbox, list) or len(item_bbox) < 4:
        return True
    iw, south, east, north = map(float, item_bbox[:4])
    west, query_south, query_east, query_north = bounds
    return iw <= query_east and east >= west and south <= query_north and north >= query_south


def _https_asset(item: dict, preferred: str | None = None) -> str | None:
    assets = item.get("assets") or {}
    if preferred and isinstance(assets.get(preferred), dict):
        href = assets[preferred].get("href")
        if isinstance(href, str) and href.startswith("https://"):
            return href
    for asset in assets.values():
        if not isinstance(asset, dict):
            continue
        href = asset.get("href")
        media_type = str(asset.get("type") or "").lower()
        if (
            isinstance(href, str)
            and href.startswith("https://")
            and ("tiff" in media_type or "geotiff" in media_type)
        ):
            return href
    return None


def _scene_key(provider: str, item_id: str, asset_url: str) -> str:
    digest = hashlib.sha256(asset_url.encode("utf-8")).hexdigest()[:12]
    return f"{provider}:{item_id}:{digest}"


def _remember(scene: dict) -> dict:
    try:
        workspace = current().workspace
    except RuntimeError:
        workspace = None
    workspace_key = str(workspace.root.resolve()) if workspace is not None else "<unbound>"
    with _cache_lock:
        _scene_cache[(workspace_key, scene["scene_key"])] = dict(scene)
        try:
            if workspace is None:
                return scene
            path = workspace.traces / "catalog-scenes.json"
            stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(stored, dict):
                stored = {}
            stored[scene["scene_key"]] = scene
            # Keep the cache bounded while preserving the newest inserted keys.
            stored = dict(list(stored.items())[-500:])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(stored, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except (OSError, ValueError, json.JSONDecodeError):
            # Search results are still usable in this process if cache
            # persistence is unavailable.
            pass
    return scene


def _cached_scene(scene_key: str) -> dict:
    workspace_key = str(current().workspace.root.resolve())
    with _cache_lock:
        scene = _scene_cache.get((workspace_key, scene_key))
        if scene:
            return dict(scene)
        try:
            path = current().workspace.traces / "catalog-scenes.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            scene = stored.get(scene_key) if isinstance(stored, dict) else None
            if isinstance(scene, dict):
                _scene_cache[(workspace_key, scene_key)] = dict(scene)
                return dict(scene)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {}


def search_vantor_imagery(
    event_id: str,
    bounds: list[float] | None = None,
    phase: str = "all",
    limit: int = 30,
) -> list[dict]:
    """List renderable Vantor scenes for an event, optionally filtered by AOI.

    ``bounds`` is ``[west, south, east, north]`` and ``phase`` is ``all``,
    ``pre``, or ``post``. Results contain stable scene keys, thumbnails, dates,
    resolution, cloud cover, and source COG URLs.
    """
    if bounds is not None and (
        len(bounds) != 4 or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]
    ):
        raise ValueError("bounds must be [west, south, east, north]")
    if phase not in {"all", "pre", "post"}:
        raise ValueError("phase must be all, pre, or post")
    catalog = _fetch_json(_VANTOR_CATALOG)
    selected = None
    for link in _links(catalog, "child"):
        candidate = _event_id(_VANTOR_CATALOG, link)
        if candidate == event_id or str(link.get("title") or "") == event_id:
            selected = urljoin(_VANTOR_CATALOG, str(link["href"]))
            break
    if selected is None:
        raise ValueError(f"Vantor event not found: {event_id!r}")

    collection = _fetch_json(selected)
    item_urls = [urljoin(selected, str(link["href"])) for link in _links(collection, "item")]
    # Bound work even when a catalog unexpectedly contains thousands of items.
    item_urls = item_urls[:500]
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(item_urls)))) as pool:
        futures = {pool.submit(_fetch_json, url): url for url in item_urls}
        for future in as_completed(futures):
            try:
                items.append(future.result())
            except Exception:
                continue

    scenes = []
    for item in items:
        props = item.get("properties") or {}
        item_phase = str(props.get("phase") or "").lower().replace("-event", "")
        if phase != "all" and item_phase != phase:
            continue
        if not _bbox_intersects(item.get("bbox"), bounds):
            continue
        cog_url = _https_asset(item, "visual")
        if not cog_url:
            continue
        item_id = str(item.get("id") or "unknown")
        thumbnail = (item.get("assets") or {}).get("thumbnail") or {}
        scene = {
            "scene_key": _scene_key("vantor", item_id, cog_url),
            "id": item_id,
            "title": str(props.get("title") or item_id),
            "provider": "Vantor Open Data",
            "event": event_id,
            "datetime": props.get("datetime"),
            "phase": item_phase or None,
            "sensor": props.get("vehicle_name") or props.get("constellation"),
            "cloud_cover": props.get("eo:cloud_cover"),
            "gsd": props.get("pan_gsd") or props.get("multispectral_gsd"),
            "bbox": item.get("bbox"),
            "thumbnail_url": thumbnail.get("href") if isinstance(thumbnail, dict) else None,
            "asset_url": cog_url,
            "render": "cog",
        }
        scenes.append(_remember(scene))
    scenes.sort(key=lambda scene: str(scene.get("datetime") or ""), reverse=True)
    return scenes[: max(1, min(limit, 100))]


def search_openaerialmap(
    bounds: list[float],
    limit: int = 20,
    page: int = 1,
) -> dict:
    """Search OpenAerialMap using the same metadata and TiTiler contract as GeoLibre."""
    if len(bounds) != 4 or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError("bounds must be [west, south, east, north]")
    limit = max(1, min(limit, 100))
    page = max(1, page)
    query = urlencode(
        {
            "limit": limit,
            "page": page,
            "order_by": "acquisition_end",
            "sort": "desc",
            "bbox": ",".join(map(str, bounds)),
        }
    )
    body = _fetch_json(f"{_OAM_API}/meta?{query}")
    scenes = []
    for raw in body.get("results", []):
        if not isinstance(raw, dict):
            continue
        item_id = raw.get("_id") or raw.get("uuid")
        cog_url = raw.get("uuid")
        if not item_id or not isinstance(cog_url, str) or not cog_url.startswith("https://"):
            continue
        props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        tile_url = (
            f"{_OAM_TILER}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}@1x"
            f"?url={quote(cog_url, safe='')}"
        )
        scene = {
            "scene_key": _scene_key("oam", str(item_id), cog_url),
            "id": str(item_id),
            "title": str(raw.get("title") or "Untitled image"),
            "provider": str(raw.get("provider") or "OpenAerialMap"),
            "platform": raw.get("platform"),
            "datetime": raw.get("acquisition_end") or raw.get("acquisition_start"),
            "gsd": raw.get("gsd") or props.get("gsd"),
            "bbox": raw.get("bbox"),
            "thumbnail_url": props.get("thumbnail"),
            "asset_url": cog_url,
            "tile_url": tile_url,
            "render": "xyz",
        }
        scenes.append(_remember(scene))
    found = (body.get("meta") or {}).get("found", len(scenes))
    return {"scenes": scenes, "found": int(found), "page": page, "limit": limit}


def add_catalog_scene(scene_key: str, name: str | None = None) -> str:
    """Add a scene returned by a catalog search to the live map."""
    scene = _cached_scene(scene_key)
    if not scene:
        raise ValueError("scene is not in the current catalog search cache; search again")
    ctx = current()
    m = _require_map(ctx)
    layer_name = name or scene.get("title") or scene.get("id") or "Catalog scene"
    if scene.get("render") == "xyz":
        layer_id = m.add_tile_layer(scene["tile_url"], layer_name)
    else:
        layer_id = m.add_raster(scene["asset_url"], layer_name)
    for layer in m.project.get("layers", []):
        if layer.get("id") == layer_id:
            layer.setdefault("metadata", {})["geoaiCatalog"] = {
                key: scene.get(key)
                for key in (
                    "scene_key",
                    "id",
                    "provider",
                    "event",
                    "datetime",
                    "phase",
                    "sensor",
                    "gsd",
                    "cloud_cover",
                    "asset_url",
                )
                if scene.get(key) is not None
            }
            break
    _persist(ctx, m)
    return layer_id


def download_catalog_scene(scene_key: str, filename: str | None = None) -> str:
    """Download a previously searched scene into ``data/`` for local analysis."""
    scene = _cached_scene(scene_key)
    if not scene:
        raise ValueError("scene is not in the current catalog search cache; search again")
    if not filename:
        suffix = Path(urlparse(scene["asset_url"]).path).suffix or ".tif"
        filename = f"{scene.get('id') or 'catalog-scene'}{suffix}"
    return download(scene["asset_url"], filename)
