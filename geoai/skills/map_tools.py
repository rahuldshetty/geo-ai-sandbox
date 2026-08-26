"""Map tools: mutate the shared live GeoLibre map.

Every mutation persists the project to ``maps/current.geolibre.json`` so the map
survives a kernel restart and stays in sync with the workspace.
"""

from __future__ import annotations

from typing import Any

from geolibre import Map

from ..context import GeoContext, current

_SNAPSHOT = "current.geolibre.json"


def _require_map(ctx: GeoContext) -> Map:
    if ctx.map is None:
        raise RuntimeError("map not initialized")
    return ctx.map


def _persist(ctx: GeoContext, m: Map) -> None:
    m.save_project(str(ctx.workspace.maps / _SNAPSHOT))
    ctx.workspace.bump()


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
    if not _is_url(path):
        path = str(ctx.workspace.resolve(path, must_exist=True))
    rescale_arg = [list(rescale)] if rescale else None
    layer_id = m.add_raster(path, name, colormap=colormap, rescale=rescale_arg)
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
