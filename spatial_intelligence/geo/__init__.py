"""Geospatial services: raster and vector processing, and open-data catalogs.

Submodules import their heavy dependency (rasterio, geopandas, urllib) directly;
this package deliberately re-exports nothing, so importing one service never
pulls in another.
"""
