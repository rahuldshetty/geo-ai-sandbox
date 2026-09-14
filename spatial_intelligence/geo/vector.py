"""Vector services: read, reproject, buffer, clip, convert, and map layers.

Services are plain functions over a :class:`~spatial_intelligence.workspace.Workspace`
(and, for map layers, the live map). They never read the ambient runtime, so the
server and the agent path share one implementation. Writers return the written
:class:`~pathlib.Path`; recording the output in the manifest is the tool layer's
job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd

from ..map import layers as layerops
from ..map import styles
from ..workspace import Workspace

__all__ = [
    "add_vector_to_map",
    "buffer",
    "clip_vector",
    "read_vector",
    "reproject_vector",
    "to_geojson",
]


def read_vector(workspace: Workspace, path: str) -> dict:
    """Return CRS, columns, row count, bounds and geometry types."""
    gdf = gpd.read_file(str(workspace.resolve(path, must_exist=True)))
    return {
        "crs": str(gdf.crs),
        "columns": list(gdf.columns),
        "len": int(len(gdf)),
        "bounds": [float(x) for x in gdf.total_bounds],
        "geom_types": [str(t) for t in gdf.geom_type.unique().tolist()],
    }


def reproject_vector(workspace: Workspace, path: str, out: str, dst_crs: str) -> Path:
    """Reproject a vector dataset; returns the path of the written output."""
    gdf = gpd.read_file(str(workspace.resolve(path, must_exist=True)))
    target = workspace.resolve(out, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_crs(dst_crs).to_file(target)
    return target


def buffer(
    workspace: Workspace,
    path: str,
    out: str,
    distance: float,
    unit: str = "meters",
) -> Path:
    """Buffer geometries by ``distance`` (metric units use the UTM CRS)."""
    gdf = gpd.read_file(str(workspace.resolve(path, must_exist=True)))
    if unit == "meters":
        utm = gdf.estimate_utm_crs()
        result = gdf.to_crs(utm)
        result = result.copy()
        result["geometry"] = result.geometry.buffer(distance)
        result = result.to_crs(gdf.crs)
    else:
        result = gdf.copy()
        result["geometry"] = result.geometry.buffer(distance)
    target = workspace.resolve(out, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    result.to_file(target)
    return target


def clip_vector(workspace: Workspace, path: str, out: str, mask: str) -> Path:
    """Clip a vector dataset to a mask polygon layer."""
    gdf = gpd.read_file(str(workspace.resolve(path, must_exist=True)))
    mask_gdf = gpd.read_file(str(workspace.resolve(mask, must_exist=True)))
    target = workspace.resolve(out, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    gpd.clip(gdf, mask_gdf).to_file(target)
    return target


def to_geojson(workspace: Workspace, path: str, out: str) -> Path:
    """Convert a vector dataset to GeoJSON."""
    gdf = gpd.read_file(str(workspace.resolve(path, must_exist=True)))
    target = workspace.resolve(out, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(target, driver="GeoJSON")
    return target


def add_vector_to_map(
    workspace: Workspace,
    map_obj: Any,
    path: str,
    name: str,
    column: str | None = None,
    palette: str = "viridis",
) -> str:
    """Load a vector dataset and add it to the live map; returns the layer id.

    A ``column`` choropleth is computed by geolibre, which writes only the
    layer's own style; the result is mirrored into the project-level style map
    GeoLibre's app actually reads (see :mod:`spatial_intelligence.map.styles`).
    """
    name = layerops.clean_layer_name(name)
    gdf = gpd.read_file(str(workspace.resolve(path, must_exist=True)))
    if column:
        layer_id = map_obj.add_gdf(gdf, name, column=column, colormap=palette)
        styles.mirror(map_obj.project, layer_id)
        return layer_id
    return map_obj.add_gdf(gdf, name)
