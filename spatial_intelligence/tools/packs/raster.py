"""Raster tools: inspect, convert, warp, clip, stretch, and index rasters."""

from __future__ import annotations

from ...contracts.effects import Effect
from ...geo import raster
from ..runtime import ToolRuntime
from ..spec import ToolKind, pack, tool


@pack(category="raster", effects=frozenset({Effect.WORKSPACE_WRITE}))
class RasterPack:
    """Rasterio-backed raster tools; every output lands under ``results/``."""

    def __init__(self, runtime: ToolRuntime) -> None:
        self._rt = runtime

    # -- inspection (read-only) -------------------------------------------

    @tool(effects=frozenset({Effect.READ}))
    def raster_info(self, path: str) -> dict:
        """Return CRS, transform, size, bands, dtypes, nodata and bounds."""
        return raster.raster_info(self._rt.workspace, path)

    @tool(effects=frozenset({Effect.READ}))
    def raster_stats(self, path: str, band: int = 1) -> dict:
        """Return min/max/mean/std/percentiles and a 256-bin histogram for a band."""
        return raster.raster_stats(self._rt.workspace, path, band)

    @tool(effects=frozenset({Effect.READ}))
    def sample_point(self, path: str, lng: float, lat: float, band: int = 1) -> dict:
        """Sample one pixel value at a coordinate; returns ``{value, lng, lat}``."""
        return raster.sample_point(self._rt.workspace, path, lng, lat, band)

    # -- writers -----------------------------------------------------------

    @tool(kind=ToolKind.REPORTING)
    def to_cog(self, path: str, out: str, resample: str = "nearest") -> str:
        """Convert a raster to a Cloud Optimized GeoTIFF; returns the absolute path."""
        total = raster.raster_info(self._rt.workspace, path)["count"]
        job = self._rt.reporter.job(
            "raster", f"to_cog {out}", unit="steps", total=total
        )
        with job:
            written = raster.to_cog(self._rt.workspace, path, out, resample, job=job)
            job.done(artifact=self._rt.workspace.relative(written))
        return self._rt.record_artifact(written)

    @tool(kind=ToolKind.REPORTING)
    def reproject(
        self, path: str, out: str, dst_crs: str, resampling: str = "nearest"
    ) -> str:
        """Reproject a raster to ``dst_crs`` (e.g. ``"EPSG:4326"``)."""
        total = raster.raster_info(self._rt.workspace, path)["count"]
        job = self._rt.reporter.job(
            "raster", f"reproject {out}", unit="steps", total=total
        )
        with job:
            written = raster.reproject(
                self._rt.workspace, path, out, dst_crs, resampling, job=job
            )
            job.done(artifact=self._rt.workspace.relative(written))
        return self._rt.record_artifact(written)

    @tool(kind=ToolKind.REPORTING)
    def clip(
        self,
        path: str,
        out: str,
        bounds: list[float] | None = None,
        mask_geojson: str | None = None,
    ) -> str:
        """Clip a raster to ``[west,south,east,north]`` or a mask GeoJSON."""
        job = self._rt.reporter.job(
            "raster", f"clip {out}", unit="steps", total=1
        )
        with job:
            written = raster.clip(
                self._rt.workspace, path, out, bounds, mask_geojson, job=job
            )
            job.done(artifact=self._rt.workspace.relative(written))
        return self._rt.record_artifact(written)

    @tool(kind=ToolKind.REPORTING)
    def rescale(
        self,
        path: str,
        out: str,
        vmin: float | None = None,
        vmax: float | None = None,
        method: str = "percentile",
        pmin: float = 2,
        pmax: float = 98,
        nodata: float | None = None,
    ) -> str:
        """Stretch a raster to uint8. Returns the absolute path."""
        job = self._rt.reporter.job(
            "raster", f"rescale {out}", unit="steps", total=1
        )
        with job:
            written = raster.rescale(
                self._rt.workspace,
                path,
                out,
                vmin,
                vmax,
                method,
                pmin,
                pmax,
                nodata,
                job=job,
            )
            job.done(artifact=self._rt.workspace.relative(written))
        return self._rt.record_artifact(written)

    @tool(requires_approval=True)
    def band_math(
        self,
        path: str,
        out: str,
        expression: str,
        bands: dict[str, int] | None = None,
    ) -> str:
        """Evaluate a NumPy expression over named bands (e.g. an index).

        ``expression`` is evaluated with NumPy bound to ``np`` and each entry of
        ``bands`` bound to its band array, e.g. ``"(nir - red) / (nir + red)"``
        with ``bands={"nir": 4, "red": 3}``.

        This runs arbitrary NumPy code, so it only runs once the user approved
        dangerous mode (File -> Settings); prefer ``run_python`` with numpy for
        band math instead.
        """
        self._rt.require_approval(
            "band_math", hint="Use run_python with numpy for band math instead."
        )
        written = raster.band_math(
            self._rt.workspace, path, out, expression, bands
        )
        return self._rt.record_artifact(written)

    @tool()
    def gdal_translate(
        self, path: str, out: str, options: dict[str, str] | None = None
    ) -> str:
        """Low-level GDAL escape hatch via the osgeo bindings.

        Unavailable when the ``gdal`` package is not installed (no Windows wheels);
        in that case returns an explanatory message instead of raising.
        """
        written = raster.gdal_translate(self._rt.workspace, path, out, options)
        if isinstance(written, str):
            # The osgeo bindings are missing: hand the explanation back instead
            # of pretending an output exists.
            return written
        return self._rt.record_artifact(written)


__all__ = ["RasterPack"]
