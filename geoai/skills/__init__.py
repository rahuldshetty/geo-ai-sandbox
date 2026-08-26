"""Skill tools for the Geo-AI agent."""

from .map_tools import (
    add_colorbar,
    add_geojson,
    add_legend,
    add_raster,
    add_tile_layer,
    add_vector,
    add_wms,
    classify_layer,
    clear_layers,
    describe_map,
    export_html,
    fit_bounds,
    list_colormaps,
    remove_layer,
    save_map,
    set_basemap,
    set_layer_opacity,
    set_layer_visibility,
    set_view,
    style_layer,
)
from .python_tools import run_python
from .raster_tools import (
    band_math,
    clip,
    gdal_translate,
    raster_info,
    raster_stats,
    reproject,
    rescale,
    sample_point,
    to_cog,
)
from .vector_tools import (
    add_vector_to_map,
    buffer,
    clip_vector,
    read_vector,
    reproject_vector,
    to_geojson,
)
from .workspace_tools import (
    download,
    find_files,
    import_data,
    list_files,
    read_file,
    write_file,
)

# Every tool registered on the agent, in one explicit list (no dynamic import).
ALL_TOOLS = [
    # map
    describe_map,
    list_colormaps,
    add_geojson,
    add_vector,
    add_raster,
    add_tile_layer,
    add_wms,
    set_view,
    set_basemap,
    style_layer,
    classify_layer,
    set_layer_visibility,
    set_layer_opacity,
    remove_layer,
    clear_layers,
    add_legend,
    add_colorbar,
    fit_bounds,
    save_map,
    export_html,
    # raster
    raster_info,
    raster_stats,
    to_cog,
    reproject,
    clip,
    rescale,
    band_math,
    sample_point,
    gdal_translate,
    # vector
    read_vector,
    reproject_vector,
    buffer,
    clip_vector,
    to_geojson,
    add_vector_to_map,
    # workspace
    list_files,
    find_files,
    read_file,
    write_file,
    download,
    import_data,
    # python
    run_python,
]

__all__ = ["ALL_TOOLS"]
