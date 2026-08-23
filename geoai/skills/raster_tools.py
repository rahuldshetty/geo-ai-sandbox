"""Raster tools backed by rasterio (with an osgeo escape hatch).

All outputs land under ``results/``. ``osgeo`` (the ``gdal`` package) is
optional: the ``gdal_translate`` tool degrades to an explanatory message when
the bindings are unavailable (no Windows wheels), while every other tool is
rasterio-based and always works.
"""

from __future__ import annotations

import json

import numpy as np
import rasterio
from rasterio import mask, warp
from rasterio.windows import from_bounds

from ..context import current
from ..workspace import WorkspaceError


def _src(path: str) -> rasterio.DatasetReader:
    ctx = current()
    resolved = str(ctx.workspace.resolve(path, must_exist=True))
    return rasterio.open(resolved)


def _out(path: str) -> str:
    ctx = current()
    out = ctx.workspace.resolve(path, write=True)
    return str(out)


def _record(path: str) -> str:
    ctx = current()
    out = ctx.workspace.resolve(path, write=True)
    rel = out.relative_to(ctx.workspace.root).as_posix()
    ctx.workspace.record_output(rel)
    ctx.notify()
    return str(out)


def raster_info(path: str) -> dict:
    """Return CRS, transform, size, bands, dtypes, nodata and bounds."""
    with _src(path) as ds:
        return {
            "crs": str(ds.crs),
            "transform": list(ds.transform)[:6],
            "width": ds.width,
            "height": ds.height,
            "count": ds.count,
            "dtypes": list(ds.dtypes),
            "nodata": ds.nodata,
            "bounds": list(ds.bounds),
        }


def raster_stats(path: str, band: int = 1) -> dict:
    """Return min/max/mean/std/percentiles and a 256-bin histogram for a band."""
    with _src(path) as ds:
        arr = ds.read(band, masked=True).astype("float64")
        data = arr.compressed()
        if data.size == 0:
            return {"min": None, "max": None, "mean": None, "std": None,
                    "p2": None, "p98": None, "histogram": []}
        hist, edges = np.histogram(data, bins=256)
        return {
            "min": float(data.min()),
            "max": float(data.max()),
            "mean": float(data.mean()),
            "std": float(data.std()),
            "p2": float(np.nanpercentile(data, 2)),
            "p98": float(np.nanpercentile(data, 98)),
            "histogram": [int(x) for x in hist],
        }


def to_cog(path: str, out: str, resample: str = "nearest") -> str:
    """Convert a raster to a Cloud Optimized GeoTIFF; returns the absolute path."""
    dst = _out(out)
    with rasterio.open(str(current().workspace.resolve(path, must_exist=True))) as src:
        profile = src.profile.copy()
        profile.update(
            driver="COG",
            tiled=True,
            compress="deflate",
            overview_resampling=resample,
        )
        with rasterio.open(dst, "w", **profile) as ds:
            for i in range(1, src.count + 1):
                ds.write(src.read(i), i)
    return _record(out)


def reproject(path: str, out: str, dst_crs: str, resampling: str = "nearest") -> str:
    """Reproject a raster to ``dst_crs`` (e.g. ``"EPSG:4326"``)."""
    dst = _out(out)
    with rasterio.open(str(current().workspace.resolve(path, must_exist=True))) as src:
        transform, width, height = warp.calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        kwargs = src.meta.copy()
        kwargs.update(
            crs=dst_crs, transform=transform, width=width, height=height,
            driver="GTiff", compress="deflate",
        )
        with rasterio.open(dst, "w", **kwargs) as ds:
            for i in range(1, src.count + 1):
                warp.reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(ds, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=getattr(warp.Resampling, resampling, warp.Resampling.nearest),
                )
    return _record(out)


def clip(
    path: str,
    out: str,
    bounds: list[float] | None = None,
    mask_geojson: str | None = None,
) -> str:
    """Clip a raster to ``[west,south,east,north]`` or a mask GeoJSON."""
    dst = _out(out)
    ctx = current()
    with rasterio.open(str(ctx.workspace.resolve(path, must_exist=True))) as src:
        if bounds is not None:
            window = from_bounds(*bounds, transform=src.transform)
            kwargs = src.meta.copy()
            kwargs.update(
                height=window.height,
                width=window.width,
                transform=rasterio.windows.transform(window, src.transform),
                driver="GTiff",
                compress="deflate",
            )
            with rasterio.open(dst, "w", **kwargs) as ds:
                ds.write(src.read(window=window))
        elif mask_geojson is not None:
            mask_path = str(ctx.workspace.resolve(mask_geojson, must_exist=True))
            with open(mask_path, encoding="utf-8") as f:
                geoms = json.load(f)
            if "features" in geoms:
                geoms = [feat["geometry"] for feat in geoms["features"]]
            else:
                geoms = [geoms]
            out_image, out_transform = mask.mask(src, geoms, crop=True)
            kwargs = src.meta.copy()
            kwargs.update(
                height=out_image.shape[1],
                width=out_image.shape[2],
                transform=out_transform,
                driver="GTiff",
                compress="deflate",
            )
            with rasterio.open(dst, "w", **kwargs) as ds:
                ds.write(out_image)
        else:
            raise WorkspaceError("clip requires either bounds or mask_geojson")
    return _record(out)


def rescale(
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
    dst = _out(out)
    with rasterio.open(str(current().workspace.resolve(path, must_exist=True))) as src:
        arr = src.read(1).astype("float64")
        if nodata is not None and src.nodata is not None:
            arr = np.where(arr == src.nodata, np.nan, arr)
        if vmin is None or vmax is None:
            if method == "percentile":
                valid = arr[np.isfinite(arr)]
                vmin = vmin if vmin is not None else float(np.nanpercentile(valid, pmin))
                vmax = vmax if vmax is not None else float(np.nanpercentile(valid, pmax))
            else:
                valid = arr[np.isfinite(arr)]
                vmin = vmin if vmin is not None else float(valid.min())
                vmax = vmax if vmax is not None else float(valid.max())
        scaled = np.clip((arr - vmin) / (vmax - vmin) * 255.0, 0, 255).astype("uint8")
        if nodata is not None:
            scaled = np.where(np.isnan(arr), 0, scaled)
        profile = src.profile.copy()
        profile.update(dtype="uint8", count=1, driver="GTiff", compress="deflate")
        if nodata is not None:
            profile.update(nodata=0)
        with rasterio.open(dst, "w", **profile) as ds:
            ds.write(scaled, 1)
    return _record(out)


def band_math(
    path: str, out: str, expression: str, bands: dict[str, int] | None = None
) -> str:
    """Evaluate a NumPy expression over named bands (e.g. an index)."""
    dst = _out(out)
    with rasterio.open(str(current().workspace.resolve(path, must_exist=True))) as src:
        band_arrays: dict[str, np.ndarray] = {}
        for name, idx in (bands or {}).items():
            band_arrays[name] = src.read(idx).astype("float64")
        result = eval(expression, {"np": np}, band_arrays)  # noqa: S307 - tool contract
        result = np.asarray(result, dtype="float64")
        profile = src.profile.copy()
        profile.update(dtype=result.dtype.name, count=1, driver="GTiff", compress="deflate")
        with rasterio.open(dst, "w", **profile) as ds:
            ds.write(result, 1)
    return _record(out)


def sample_point(path: str, lng: float, lat: float, band: int = 1) -> dict:
    """Sample one pixel value at a coordinate; returns ``{value, lng, lat}``."""
    with _src(path) as ds:
        values = list(ds.sample([(lng, lat)], indexes=[band]))
        value = values[0][0] if values else None
        if value is not None:
            value = float(value)
        return {"value": value, "lng": lng, "lat": lat}


def gdal_translate(path: str, out: str, options: dict[str, str] | None = None) -> str:
    """Low-level GDAL escape hatch via the osgeo bindings.

    Unavailable when the ``gdal`` package is not installed (no Windows wheels);
    in that case returns an explanatory message instead of raising.
    """
    try:
        from osgeo import gdal as osgeo_gdal
    except ImportError:  # pragma: no cover - environment-dependent
        return (
            "gdal_translate unavailable: the 'gdal' (osgeo) package is not installed. "
            "All other raster tools (rasterio-based) work normally."
        )
    dst = _out(out)
    src = osgeo_gdal.Open(str(current().workspace.resolve(path, must_exist=True)))
    if src is None:
        raise WorkspaceError(f"gdal could not open source raster: {path!r}")
    result = osgeo_gdal.Translate(dst, src, **(options or {}))
    del result, src
    return _record(out)
