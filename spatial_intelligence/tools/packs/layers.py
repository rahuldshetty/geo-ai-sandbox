"""Map tools: mutate the shared live GeoLibre map.

Every mutation persists the project to ``maps/current.geolibre.json`` so the map
survives a kernel restart and stays in sync with the workspace.
"""

from __future__ import annotations

from typing import Any

from ...contracts.effects import Effect
from ...map import bridge
from ...map import layers as layerops
from ..runtime import ToolRuntime
from ..spec import pack, tool


@pack(category="layers", effects=frozenset({Effect.MAP_WRITE}))
class LayersPack:
    """Map tools; every mutation persists the workspace snapshot."""

    def __init__(self, runtime: ToolRuntime) -> None:
        self._rt = runtime

    @property
    def _map(self):
        """The session's live map, raising when the session has none."""
        return self._rt.require_map()

    def _mutated(self) -> None:
        """Tell the UI the live map project changed."""
        self._rt.events.notify_map()

    # -- read-only ---------------------------------------------------------

    @tool(core=True, effects=frozenset({Effect.READ}))
    def describe_map(self) -> dict:
        """Return a compact summary of the current map (layers, view, basemap)."""
        return layerops.describe(self._map)

    @tool(core=True, effects=frozenset({Effect.READ}))
    def describe_geolibre_bridge(self) -> dict:
        """Return the connected GeoLibre version and callable embed methods.

        Plugin methods are not advertised by GeoLibre 2.9's embed protocol yet;
        use capability discovery for an explicit interactive handoff instead of
        assuming that a plugin can be called programmatically.
        """
        return bridge.bridge_info()

    @tool(effects=frozenset({Effect.READ}))
    def list_colormaps(self) -> dict[str, list[str]]:
        """Return the named color ramps valid for ``colormap``/``palette`` arguments.

        Each key is a valid ``colormap`` value for ``add_raster``/``add_colorbar``
        and a valid ``palette`` value for ``classify_layer``/``add_vector_to_map``;
        the value is the ramp's anchor CSS colors. Prefer this over guessing a name.
        """
        return layerops.list_colormaps()

    # -- layer writes ------------------------------------------------------

    @tool()
    def add_geojson(self, data: str, name: str, style: dict[str, Any] | None = None) -> str:
        """Add a GeoJSON layer and return its id.

        ``data`` may be a workspace-relative path, an http(s) URL, or a literal
        GeoJSON string.
        """
        layer_id = layerops.add_geojson(
            self._rt.workspace, self._map, data, name, style=style
        )
        self._mutated()
        return layer_id

    @tool()
    def add_vector(
        self,
        data: str,
        name: str,
        data_format: str | None = None,
        source_layer: str | None = None,
    ) -> str:
        """Add a vector layer from a path/URL and return its id."""
        layer_id = layerops.add_vector(
            self._rt.workspace,
            self._map,
            data,
            name,
            data_format=data_format,
            source_layer=source_layer,
        )
        self._mutated()
        return layer_id

    @tool()
    def add_raster(
        self,
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
        layer_id = layerops.add_raster(
            self._rt.workspace,
            self._map,
            path,
            name,
            colormap=colormap,
            rescale=rescale,
            file_url=self._rt.file_url,
        )
        self._mutated()
        return layer_id

    @tool()
    def add_tile_layer(self, url: str, name: str, attribution: str | None = None) -> str:
        """Add an XYZ tile layer and return its id."""
        layer_id = layerops.add_tile_layer(
            self._rt.workspace, self._map, url, name, attribution
        )
        self._mutated()
        return layer_id

    @tool()
    def add_wms(
        self, endpoint: str, layers: str, name: str, styles: str | None = None
    ) -> str:
        """Add a WMS tiled layer and return its id."""
        layer_id = layerops.add_wms(
            self._rt.workspace, self._map, endpoint, layers, name, styles=styles
        )
        self._mutated()
        return layer_id

    # -- view --------------------------------------------------------------

    @tool()
    def set_view(self, center: list[float] | None = None, zoom: float | None = None) -> dict:
        """Center/zoom the map. ``center`` is ``[lng, lat]``."""
        result = layerops.set_view(self._rt.workspace, self._map, center, zoom)
        self._mutated()
        return result

    @tool()
    def set_basemap(self, basemap: str) -> dict:
        """Set the background basemap (name or MapLibre style URL)."""
        result = layerops.set_basemap(self._rt.workspace, self._map, basemap)
        self._mutated()
        return result

    @tool()
    def fit_bounds(self, bounds: list[float]) -> dict:
        """Fit the map camera and return confirmation of the resulting view."""
        result = layerops.fit_bounds(self._rt.workspace, self._map, bounds)
        self._mutated()
        return result

    # -- styling -----------------------------------------------------------

    @tool()
    def style_layer(self, layer: str, style: dict[str, Any]) -> dict:
        """Merge style overrides onto a layer (e.g. ``{"fillColor": "#ff0000"}``)."""
        result = layerops.style_layer(self._rt.workspace, self._map, layer, style)
        self._mutated()
        return result

    @tool()
    def classify_layer(
        self,
        layer: str,
        column: str,
        palette: str = "viridis",
        method: str = "quantile",
        k: int = 5,
    ) -> dict:
        """Symbolize a GeoJSON layer as a choropleth on a numeric ``column``.

        ``method`` is ``"quantile"`` or ``"equal-interval"``; ``k`` is the class
        count; ``palette`` is a color-ramp name.
        """
        result = layerops.classify_layer(
            self._rt.workspace, self._map, layer, column, palette, method, k
        )
        self._mutated()
        return result

    @tool()
    def set_layer_visibility(self, layer: str, visible: bool) -> dict:
        """Show or hide a layer."""
        result = layerops.set_layer_visibility(
            self._rt.workspace, self._map, layer, visible
        )
        self._mutated()
        return result

    @tool()
    def set_layer_opacity(self, layer: str, opacity: float) -> dict:
        """Set a layer's opacity in ``[0, 1]``."""
        result = layerops.set_layer_opacity(
            self._rt.workspace, self._map, layer, opacity
        )
        self._mutated()
        return result

    @tool()
    def remove_layer(self, layer: str) -> dict:
        """Remove a layer by id or display name."""
        result = layerops.remove_layer(self._rt.workspace, self._map, layer)
        self._mutated()
        return result

    @tool()
    def clear_layers(self) -> dict:
        """Remove all layers from the map."""
        result = layerops.clear_layers(self._rt.workspace, self._map)
        self._mutated()
        return result

    @tool()
    def add_legend(
        self,
        title: str | None = None,
        items: dict[str, str] | None = None,
        shape: str = "square",
    ) -> dict:
        """Add a legend. ``items`` maps label -> CSS color."""
        result = layerops.add_legend(
            self._rt.workspace, self._map, title, items, shape
        )
        self._mutated()
        return result

    @tool()
    def add_colorbar(
        self, colormap: str = "viridis", vmin: float = 0.0, vmax: float = 1.0
    ) -> dict:
        """Add a colorbar for a continuous (single-band) raster.

        ``colormap`` is one of the names from :func:`list_colormaps`.
        """
        result = layerops.add_colorbar(
            self._rt.workspace, self._map, colormap, vmin, vmax
        )
        self._mutated()
        return result

    # -- files -------------------------------------------------------------

    @tool(effects=frozenset({Effect.WORKSPACE_WRITE}))
    def save_map(self, path: str) -> str:
        """Save the current project under ``maps/``; returns the absolute path."""
        out = layerops.save_map(self._rt.workspace, self._map, path)
        return self._rt.record_artifact(out)

    @tool(effects=frozenset({Effect.WORKSPACE_WRITE}))
    def export_html(self, path: str, title: str = "GeoLibre Map") -> str:
        """Export the map as a standalone HTML page under ``results/``."""
        out = layerops.export_html(self._rt.workspace, self._map, path, title)
        return self._rt.record_artifact(out)


__all__ = ["LayersPack"]
