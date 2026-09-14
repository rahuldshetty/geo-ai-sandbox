"""Vector tools backed by GeoPandas. Outputs land under ``results/``."""

from __future__ import annotations

from ...contracts.effects import Effect
from ...geo import vector
from ...map import document
from ..runtime import ToolRuntime
from ..spec import pack, tool


@pack(category="vector", effects=frozenset({Effect.WORKSPACE_WRITE}))
class VectorPack:
    """GeoPandas vector tools; every written output is recorded in the manifest."""

    def __init__(self, runtime: ToolRuntime) -> None:
        self._rt = runtime

    @tool(effects=frozenset({Effect.READ}))
    def read_vector(self, path: str) -> dict:
        """Return CRS, columns, row count, bounds and geometry types."""
        return vector.read_vector(self._rt.workspace, path)

    @tool()
    def reproject_vector(self, path: str, out: str, dst_crs: str) -> str:
        """Reproject a vector dataset; returns the absolute output path."""
        written = vector.reproject_vector(self._rt.workspace, path, out, dst_crs)
        return self._rt.record_artifact(written)

    @tool()
    def buffer(self, path: str, out: str, distance: float, unit: str = "meters") -> str:
        """Buffer geometries by ``distance`` (metric units use the UTM CRS)."""
        written = vector.buffer(self._rt.workspace, path, out, distance, unit)
        return self._rt.record_artifact(written)

    @tool()
    def clip_vector(self, path: str, out: str, mask: str) -> str:
        """Clip a vector dataset to a mask polygon layer."""
        written = vector.clip_vector(self._rt.workspace, path, out, mask)
        return self._rt.record_artifact(written)

    @tool()
    def to_geojson(self, path: str, out: str) -> str:
        """Convert a vector dataset to GeoJSON."""
        written = vector.to_geojson(self._rt.workspace, path, out)
        return self._rt.record_artifact(written)

    @tool(effects=frozenset({Effect.MAP_WRITE}))
    def add_vector_to_map(
        self, path: str, name: str, column: str | None = None, palette: str = "viridis"
    ) -> str:
        """Load a vector dataset and add it to the live map; returns the layer id."""
        m = self._rt.require_map()
        layer_id = vector.add_vector_to_map(
            self._rt.workspace, m, path, name, column=column, palette=palette
        )
        document.persist_map(m, self._rt.workspace)
        self._rt.events.notify_map()
        return layer_id


__all__ = ["VectorPack"]
