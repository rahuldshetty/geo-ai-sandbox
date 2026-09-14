"""OpenAerialMap search, rendered through the HOT TiTiler service.

OpenAerialMap's ``/meta`` endpoint returns imagery metadata; each result's
``uuid`` is itself an HTTPS COG, which TiTiler renders as an XYZ layer. The
metadata and render URL follow the same contract GeoLibre's OpenAerialMap
plugin uses, so the two front ends describe one scene identically.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from ...contracts.errors import ToolInputError
from .base import fetch_json
from .scenes import SceneCache, scene_key

#: OpenAerialMap metadata API.
OAM_API = "https://api.openaerialmap.org"
#: HOT's TiTiler deployment used for the tile URLs.
OAM_TILER = "https://titiler.hotosm.org"


def search_openaerialmap(
    bounds: list[float],
    limit: int = 20,
    page: int = 1,
    *,
    cache: SceneCache,
) -> dict:
    """Search OpenAerialMap using the same metadata and TiTiler contract as GeoLibre."""
    if len(bounds) != 4 or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ToolInputError("bounds must be [west, south, east, north]")
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
    body = fetch_json(f"{OAM_API}/meta?{query}")
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
            f"{OAM_TILER}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}@1x"
            f"?url={quote(cog_url, safe='')}"
        )
        scene = {
            "scene_key": scene_key("oam", str(item_id), cog_url),
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
        scenes.append(cache.remember(scene))
    found = (body.get("meta") or {}).get("found", len(scenes))
    return {"scenes": scenes, "found": int(found), "page": page, "limit": limit}


__all__ = ["OAM_API", "OAM_TILER", "search_openaerialmap"]
