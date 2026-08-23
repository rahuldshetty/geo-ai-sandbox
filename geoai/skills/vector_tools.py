"""Vector tools backed by GeoPandas. Outputs land under ``results/``."""

from __future__ import annotations

import geopandas as gpd

from ..context import current
from .map_tools import _persist, _require_map


def _record(path: str) -> str:
    ctx = current()
    out = ctx.workspace.resolve(path, write=True)
    rel = out.relative_to(ctx.workspace.root).as_posix()
    ctx.workspace.record_output(rel)
    ctx.notify()
    return str(out)


def read_vector(path: str) -> dict:
    """Return CRS, columns, row count, bounds and geometry types."""
    ctx = current()
    gdf = gpd.read_file(str(ctx.workspace.resolve(path, must_exist=True)))
    return {
        "crs": str(gdf.crs),
        "columns": list(gdf.columns),
        "len": int(len(gdf)),
        "bounds": [float(x) for x in gdf.total_bounds],
        "geom_types": [str(t) for t in gdf.geom_type.unique().tolist()],
    }


def reproject_vector(path: str, out: str, dst_crs: str) -> str:
    """Reproject a vector dataset; returns the absolute output path."""
    ctx = current()
    gdf = gpd.read_file(str(ctx.workspace.resolve(path, must_exist=True)))
    gdf.to_crs(dst_crs).to_file(ctx.workspace.resolve(out, write=True))
    return _record(out)


def buffer(path: str, out: str, distance: float, unit: str = "meters") -> str:
    """Buffer geometries by ``distance`` (metric units use the UTM CRS)."""
    ctx = current()
    gdf = gpd.read_file(str(ctx.workspace.resolve(path, must_exist=True)))
    if unit == "meters":
        utm = gdf.estimate_utm_crs()
        result = gdf.to_crs(utm)
        result = result.copy()
        result["geometry"] = result.geometry.buffer(distance)
        result = result.to_crs(gdf.crs)
    else:
        result = gdf.copy()
        result["geometry"] = result.geometry.buffer(distance)
    result.to_file(ctx.workspace.resolve(out, write=True))
    return _record(out)


def clip_vector(path: str, out: str, mask: str) -> str:
    """Clip a vector dataset to a mask polygon layer."""
    ctx = current()
    gdf = gpd.read_file(str(ctx.workspace.resolve(path, must_exist=True)))
    mask_gdf = gpd.read_file(str(ctx.workspace.resolve(mask, must_exist=True)))
    result = gpd.clip(gdf, mask_gdf)
    result.to_file(ctx.workspace.resolve(out, write=True))
    return _record(out)


def to_geojson(path: str, out: str) -> str:
    """Convert a vector dataset to GeoJSON."""
    ctx = current()
    gdf = gpd.read_file(str(ctx.workspace.resolve(path, must_exist=True)))
    gdf.to_file(ctx.workspace.resolve(out, write=True), driver="GeoJSON")
    return _record(out)


def add_vector_to_map(
    path: str, name: str, column: str | None = None, palette: str = "viridis"
) -> str:
    """Load a vector dataset and add it to the live map; returns the layer id."""
    ctx = current()
    m = _require_map(ctx)
    gdf = gpd.read_file(str(ctx.workspace.resolve(path, must_exist=True)))
    if column:
        layer_id = m.add_gdf(gdf, name, column=column, colormap=palette)
    else:
        layer_id = m.add_gdf(gdf, name)
    _persist(ctx, m)
    return layer_id
