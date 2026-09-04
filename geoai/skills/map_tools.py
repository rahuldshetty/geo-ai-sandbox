"""Map tools: mutate the shared live GeoLibre map.

Every mutation persists the project to ``maps/current.geolibre.json`` so the map
survives a kernel restart and stays in sync with the workspace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from geolibre import Map

from ..context import GeoContext, current
from ..workspace import Workspace, WorkspaceError

_SNAPSHOT = "current.geolibre.json"
_LOCAL_SOURCE_KEY = "geoaiSourcePath"


def _require_map(ctx: GeoContext) -> Map:
    if ctx.map is None:
        raise RuntimeError("map not initialized")
    return ctx.map


def _persist(ctx: GeoContext, m: Map) -> None:
    m.save_project(str(ctx.workspace.maps / _SNAPSHOT))
    ctx.workspace.bump()


def _tag_local_source(m: Map, layer_id: str, rel: str) -> None:
    """Record the workspace-relative path of a locally-served raster layer."""
    for layer in m.project.get("layers", []):
        if layer.get("id") == layer_id:
            layer.setdefault("metadata", {})[_LOCAL_SOURCE_KEY] = rel
            return


def _local_rel_for_layer(layer: dict, ws: Workspace) -> str | None:
    """Return the workspace-relative path of a locally-served raster layer.

    Prefers the ``geoaiSourcePath`` metadata tag. Falls back to recovering the
    path from a session URL — either the old ``_geolibre_local/...`` scheme (by
    filename under ``results/``/``data/``/``maps/``) or a ``/api/files/<rel>``
    URL saved under a previous server port — so projects persist across restarts
    and port changes.
    """
    meta = layer.get("metadata") or {}
    rel = meta.get(_LOCAL_SOURCE_KEY)
    if rel:
        return rel
    raw = layer.get("sourcePath")
    if not isinstance(raw, str):
        source = layer.get("source")
        if isinstance(source, dict):
            raw = source.get("url")
    if not isinstance(raw, str):
        return None
    if "_geolibre_local/" in raw:
        name = Path(urlparse(raw).path).name
        if not name:
            return None
        for sub in ("results", "data", "maps"):
            rel = f"{sub}/{name}"
            try:
                candidate = ws.resolve(rel, must_exist=True)
            except WorkspaceError:
                continue
            if candidate.is_file():
                return rel
        return None
    if "/api/files/" in raw:
        rel = unquote(urlparse(raw).path.split("/api/files/", 1)[1])
        try:
            candidate = ws.resolve(rel, must_exist=True)
        except WorkspaceError:
            return None
        return rel if candidate.is_file() else None
    return None


def repoint_local_rasters(m: Map, ws: Workspace) -> int:
    """Re-point local raster layers to the stable Geo-AI file route.

    ``add_raster`` embeds a stable ``/api/files/<rel>`` URL served by this
    harness's own server (with CORS), so local rasters survive a server restart
    without the geolibre static server's per-session token. This migrates layers
    saved with the old ``_geolibre_local/...`` session URL back to the stable URL
    (recovering the workspace path from ``metadata.geoaiSourcePath`` or, when
    that tag is missing, by filename). Returns the number of layers re-pointed.
    """
    ctx = current()
    count = 0
    for layer in m.project.get("layers", []):
        rel = _local_rel_for_layer(layer, ws)
        if not rel:
            continue
        url = ctx.file_url(rel)
        source = layer.get("source")
        if isinstance(source, dict):
            source["url"] = url
        layer["sourcePath"] = url
        layer.setdefault("metadata", {})[_LOCAL_SOURCE_KEY] = rel
        layer["metadata"].pop("error", None)
        count += 1
    return count


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _is_geojson_literal(s: str) -> bool:
    return s.lstrip().startswith(("{", "["))


def _record(ctx: GeoContext, out: Any) -> str:
    rel = out.relative_to(ctx.workspace.root).as_posix()
    ctx.workspace.record_output(rel)
    ctx.notify()
    return str(out)


def describe_map() -> dict:
    """Return a compact summary of the current map (layers, view, basemap)."""
    return _require_map(current()).describe()


def list_colormaps() -> dict[str, list[str]]:
    """Return the named color ramps valid for ``colormap``/``palette`` arguments.

    Each key is a valid ``colormap`` value for ``add_raster``/``add_colorbar``
    and a valid ``palette`` value for ``classify_layer``/``add_vector_to_map``;
    the value is the ramp's anchor CSS colors. Prefer this over guessing a name.
    """
    from geolibre.color_ramp import VECTOR_COLOR_RAMPS

    return {name: list(colors) for name, colors in VECTOR_COLOR_RAMPS.items()}


def add_geojson(data: str, name: str, style: dict[str, Any] | None = None) -> str:
    """Add a GeoJSON layer and return its id.

    ``data`` may be a workspace-relative path, an http(s) URL, or a literal
    GeoJSON string.
    """
    ctx = current()
    m = _require_map(ctx)
    if not _is_url(data) and not _is_geojson_literal(data):
        data = str(ctx.workspace.resolve(data, must_exist=True))
    layer_id = m.add_geojson(data, name, **(style or {}))
    _persist(ctx, m)
    return layer_id


def add_vector(
    data: str,
    name: str,
    data_format: str | None = None,
    source_layer: str | None = None,
) -> str:
    """Add a vector layer from a path/URL and return its id."""
    ctx = current()
    m = _require_map(ctx)
    if not _is_url(data):
        data = str(ctx.workspace.resolve(data, must_exist=True))
    layer_id = m.add_vector(data, name, data_format=data_format, source_layer=source_layer)
    _persist(ctx, m)
    return layer_id


def add_raster(
    path: str,
    name: str,
    colormap: str | None = None,
    rescale: list[float] | None = None,
) -> str:
    """Add a raster (COG/GeoTIFF) layer and return its id.

    ``rescale`` is a ``[min, max]`` stretch for a single band. ``colormap`` is
    one of the names from :func:`list_colormaps` (e.g. ``"viridis"``, ``"gray"``,
    ``"blues"``, ``"terrain"``); omit it to render the raw values.
    """
    ctx = current()
    m = _require_map(ctx)
    rel = None
    if not _is_url(path):
        rel = path
        # Validate confinement/existence, then serve from this server's own
        # origin (stable, CORS-enabled) instead of the geolibre static server's
        # per-session token URL.
        ctx.workspace.resolve(path, must_exist=True)
        path = ctx.file_url(rel)
    rescale_arg = [list(rescale)] if rescale else None
    layer_id = m.add_raster(path, name, colormap=colormap, rescale=rescale_arg)
    if rel is not None:
        _tag_local_source(m, layer_id, rel)
    _persist(ctx, m)
    return layer_id


def add_tile_layer(url: str, name: str, attribution: str | None = None) -> str:
    """Add an XYZ tile layer and return its id."""
    ctx = current()
    m = _require_map(ctx)
    layer_id = m.add_tile_layer(url, name, attribution=attribution)
    _persist(ctx, m)
    return layer_id


def add_wms(
    endpoint: str, layers: str, name: str, styles: str | None = None
) -> str:
    """Add a WMS tiled layer and return its id."""
    ctx = current()
    m = _require_map(ctx)
    layer_id = m.add_wms(endpoint, layers, name, styles=styles)
    _persist(ctx, m)
    return layer_id


def set_view(center: list[float] | None = None, zoom: float | None = None) -> None:
    """Center/zoom the map. ``center`` is ``[lng, lat]``."""
    ctx = current()
    m = _require_map(ctx)
    if center is not None:
        m.set_center(center[0], center[1], zoom=zoom)
    elif zoom is not None:
        m.set_zoom(zoom)
    _persist(ctx, m)


def set_basemap(basemap: str) -> None:
    """Set the background basemap (name or MapLibre style URL)."""
    ctx = current()
    m = _require_map(ctx)
    m.set_basemap(basemap)
    _persist(ctx, m)


def style_layer(layer: str, style: dict[str, Any]) -> None:
    """Merge style overrides onto a layer (e.g. ``{"fillColor": "#ff0000"}``)."""
    ctx = current()
    m = _require_map(ctx)
    handle = m.find_layer(layer)
    if handle is None:
        raise RuntimeError(f"layer not found: {layer!r}")
    handle.set_style(**style)
    _persist(ctx, m)


def classify_layer(
    layer: str,
    column: str,
    palette: str = "viridis",
    method: str = "quantile",
    k: int = 5,
) -> None:
    """Symbolize a GeoJSON layer as a choropleth on a numeric ``column``.

    ``method`` is ``"quantile"`` or ``"equal-interval"``; ``k`` is the class
    count; ``palette`` is a color-ramp name.
    """
    ctx = current()
    m = _require_map(ctx)
    project = m.to_project()
    from geolibre.authoring import classify_layer as _classify

    _classify(project, layer, column, class_count=k, colormap=palette, scheme=method)
    m.load_project(project)
    _persist(ctx, m)


def set_layer_visibility(layer: str, visible: bool) -> None:
    """Show or hide a layer."""
    ctx = current()
    m = _require_map(ctx)
    m.set_layer_visibility(layer, visible)
    _persist(ctx, m)


def set_layer_opacity(layer: str, opacity: float) -> None:
    """Set a layer's opacity in ``[0, 1]``."""
    ctx = current()
    m = _require_map(ctx)
    m.set_layer_opacity(layer, opacity)
    _persist(ctx, m)


def remove_layer(layer: str) -> None:
    """Remove a layer by id or display name."""
    ctx = current()
    m = _require_map(ctx)
    m.remove_layer(layer)
    _persist(ctx, m)


def clear_layers() -> None:
    """Remove all layers from the map."""
    ctx = current()
    m = _require_map(ctx)
    m.clear_layers()
    _persist(ctx, m)


def add_legend(
    title: str | None = None, items: dict[str, str] | None = None, shape: str = "square"
) -> None:
    """Add a legend. ``items`` maps label -> CSS color."""
    ctx = current()
    m = _require_map(ctx)
    m.add_legend(title=title, legend_dict=items, shape=shape)
    _persist(ctx, m)


def add_colorbar(colormap: str = "viridis", vmin: float = 0.0, vmax: float = 1.0) -> None:
    """Add a colorbar for a continuous (single-band) raster.

    ``colormap`` is one of the names from :func:`list_colormaps`.
    """
    ctx = current()
    m = _require_map(ctx)
    m.add_colorbar(colormap=colormap, vmin=vmin, vmax=vmax)
    _persist(ctx, m)


def zoom_to_layer(layer: str) -> None:
    """Fit the map camera to a layer (requires the live map)."""
    ctx = current()
    m = _require_map(ctx)
    m.zoom_to_layer(layer)


def fit_bounds(bounds: list[float]) -> None:
    """Fit the map camera to ``[west, south, east, north]`` (pure project mutation)."""
    ctx = current()
    m = _require_map(ctx)
    m.fit_project_bounds(bounds)
    _persist(ctx, m)


def save_map(path: str) -> str:
    """Save the current project under ``maps/``; returns the absolute path."""
    ctx = current()
    m = _require_map(ctx)
    out = ctx.workspace.resolve_under(ctx.workspace.maps, path)
    m.save_project(str(out))
    return _record(ctx, out)


def export_html(path: str, title: str = "GeoLibre Map") -> str:
    """Export the map as a standalone HTML page under ``results/``."""
    ctx = current()
    m = _require_map(ctx)
    out = ctx.workspace.resolve_under(ctx.workspace.results, path)
    m.to_html(str(out), title=title)
    return _record(ctx, out)
