"""Compact capability discovery without exposing every integration as a tool."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the module import-cheap
    from .tools.registry import ToolRegistry


@dataclass(frozen=True)
class Capability:
    """One thing the harness can do, and how the agent reaches it."""

    id: str
    title: str
    summary: str
    keywords: tuple[str, ...]
    implementation: str
    tools: tuple[str, ...] = ()
    geolibre_plugins: tuple[str, ...] = ()
    status: str = "available"
    fallback: str | None = None


# This is intentionally metadata, not another set of model-visible tool schemas.
# It lets the agent find a relevant capability first; executable packs can later
# be injected dynamically as the GeoLibre bridge grows.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="workspace.files",
        title="Workspace files",
        summary="Import, download, find, inspect, and write workspace data.",
        keywords=("file", "import", "download", "workspace", "data"),
        implementation="backend",
        tools=("list_files", "find_files", "read_file", "write_file", "download", "download_files"),
    ),
    Capability(
        id="map.layers",
        title="Map layers and styling",
        summary="Add and style raster, vector, tile, and WMS layers.",
        keywords=("map", "plot", "display", "layer", "style", "raster", "vector"),
        implementation="backend+geolibre",
        tools=("add_raster", "add_vector", "add_geojson", "style_layer", "fit_bounds"),
    ),
    Capability(
        id="raster.processing",
        title="Raster processing",
        summary="Inspect, clip, reproject, rescale, calculate bands, and create COGs.",
        keywords=("raster", "imagery", "satellite", "clip", "reproject", "cog", "band"),
        implementation="backend",
        tools=("raster_info", "clip", "reproject", "rescale", "band_math", "to_cog"),
    ),
    Capability(
        id="vector.processing",
        title="Vector processing",
        summary="Inspect, reproject, buffer, clip, and export vector datasets.",
        keywords=("vector", "building", "road", "polygon", "buffer", "geojson"),
        implementation="backend",
        tools=("read_vector", "reproject_vector", "buffer", "clip_vector", "to_geojson"),
    ),
    Capability(
        id="catalog.disaster-imagery",
        title="Open disaster imagery",
        summary="Search, download into data/, and map Vantor and OpenAerialMap disaster imagery.",
        keywords=(
            "disaster", "flood", "landslide", "earthquake", "before", "after",
            "planet", "vantor", "aerial", "stac", "imagery",
        ),
        implementation="backend using GeoLibre catalog contracts",
        tools=(
            "search_vantor_events",
            "search_vantor_imagery",
            "search_openaerialmap",
            "add_catalog_scene",
            "download_catalog_scene",
        ),
        geolibre_plugins=(
            "Vantor Open Data",
            "OpenAerialMap",
        ),
        status="available",
    ),
    Capability(
        id="catalog.planet-stac",
        title="Planet Open Data and generic STAC",
        summary="Discover Planet disaster releases or another STAC catalog.",
        keywords=("disaster", "planet", "stac", "satellite", "catalog", "imagery"),
        implementation="geolibre-plugin",
        geolibre_plugins=(
            "Planet Open Data",
            "STAC Catalogs",
        ),
        status="interactive_handoff",
        fallback="Open the matching GeoLibre Web Services panel, add selected scenes, then continue from the persisted map layers.",
    ),
    Capability(
        id="map.compare",
        title="Before/after comparison",
        summary="Compare two map layers using GeoLibre's Swipe plugin.",
        keywords=("compare", "comparison", "before", "after", "swipe", "change"),
        implementation="geolibre-plugin",
        geolibre_plugins=("Swipe",),
        status="interactive_handoff",
        fallback="Open the GeoLibre Swipe plugin and select the two persisted layers.",
    ),
    Capability(
        id="map.terrain",
        title="3D terrain",
        summary="Explore elevation and imagery in GeoLibre's terrain view.",
        keywords=("terrain", "elevation", "dem", "3d", "slope", "landslide"),
        implementation="geolibre-plugin",
        geolibre_plugins=("Terrain",),
        status="interactive_handoff",
        fallback="Enable GeoLibre's Terrain control after adding imagery.",
    ),
    Capability(
        id="catalog.overture",
        title="Overture Maps",
        summary="Find buildings, places, and transportation data for the current area.",
        keywords=("overture", "building", "infrastructure", "road", "place"),
        implementation="geolibre-plugin",
        geolibre_plugins=("Overture Maps",),
        status="interactive_handoff",
        fallback="Open the Overture Maps plugin, extract the desired theme, then analyze the added layer with vector tools.",
    ),
)


def _terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9]+", text.lower()))
    # Lightweight singular aliases are enough for deterministic routing without
    # adding a stemming/NLP dependency.
    terms.update(term[:-1] for term in list(terms) if len(term) > 3 and term.endswith("s"))
    return terms


def _ranked(goal: str, limit: int) -> list[Capability]:
    """Return the capabilities that best match ``goal``, best first."""
    terms = _terms(goal)
    ranked: list[tuple[int, Capability]] = []
    for capability in CAPABILITIES:
        haystack = _terms(" ".join((*capability.keywords, capability.title)))
        score = len(terms & haystack)
        if score:
            ranked.append((score, capability))
    if not ranked:
        ranked = [(0, capability) for capability in CAPABILITIES[:4]]
    ranked.sort(key=lambda item: (-item[0], item[1].id))
    return [capability for _, capability in ranked[: max(1, min(limit, 8))]]


def _tools_of(capability: Capability, registry: ToolRegistry | None) -> list[dict[str, Any]]:
    """Return the tools this capability names that ``registry`` actually has.

    The catalog is static metadata, so a capability may name a tool a given
    build does not ship (a GeoLibre-side integration, a pack that is not built
    yet). The registry is authoritative: unknown names are dropped rather than
    summarised speculatively. Without a registry at all, names are still
    reported, unsummarised, so the prose stays useful before the session wires
    one up.
    """
    if registry is None:
        return [{"name": name, "summary": None} for name in capability.tools]
    entries: list[dict[str, Any]] = []
    for name in capability.tools:
        if name not in registry:
            continue
        entries.append({"name": name, "summary": registry.get(name).summary})
    return entries


def discover(registry: ToolRegistry | None, goal: str, limit: int = 5) -> list[dict]:
    """Rank the capability catalog against ``goal`` and annotate it.

    Every result carries the capability's prose plus the registry summary of
    each tool it names, so the model sees what this build can actually execute
    instead of a catalog of names that may not exist here. ``registry`` may be
    ``None`` while the session is still assembling the tool surface.
    """
    return [
        {
            "id": capability.id,
            "title": capability.title,
            "summary": capability.summary,
            "keywords": list(capability.keywords),
            "implementation": capability.implementation,
            "tools": _tools_of(capability, registry),
            "geolibre_plugins": list(capability.geolibre_plugins),
            "status": capability.status,
            "fallback": capability.fallback,
        }
        for capability in _ranked(goal, limit)
    ]


__all__ = ["CAPABILITIES", "Capability", "discover"]
