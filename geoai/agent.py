"""Agent construction: system prompt + tool registration."""

from __future__ import annotations

import os

from pydantic_ai import Agent, DeferredToolRequests, ToolFailed
from pydantic_ai.capabilities import AbstractCapability, ReinjectSystemPrompt
from pydantic_ai.tools import RunContext
from pydantic_ai_harness import (
    ClearToolResults,
    Planning,
    SummarizingCompaction,
    TieredCompaction,
)

from .context import GeoContext, set_context
from pydantic_ai_harness.planning import InMemoryPlanStore
from .skills import ALL_TOOLS

SYSTEM_PROMPT = """You are GeoAI, a geospatial-analysis agent in a Geo-AI web workspace.
You control a live GeoLibre map (visible to the user) and a workspace folder.

Workspace layout — all tool paths are relative to the workspace root:
- data/     user inputs (downloads, dropped files). Read here.
- results/  your outputs (GeoTIFF/COG, GeoJSON, tables). Write here.
- maps/     saved .geolibre.json projects.

Rules:
1. For broad requests or requests involving external datasets, call
   discover_capabilities first. It identifies both executable backend tools and
   relevant GeoLibre plugins. A capability marked interactive_handoff is not
   directly callable yet: tell the user which GeoLibre panel to open and what
   to select, then continue after its layers appear on the persisted map.
2. Ask for user input with request_user_input when multiple credible datasets,
   dates, AOIs, or analysis assumptions would materially change the result.
   Prefer radio/multi-select choices with concise metadata and mark a supported
   recommendation. Do not ask about minor, cheap, reversible decisions.
   For open disaster imagery, search Vantor events and scenes (and optionally
   OpenAerialMap). If several scenes are credible, present compact scene choices
   using scene_key as each option value, thumbnail_url for previews, and date,
   phase, sensor, resolution, and cloud cover in the description. After the user
   chooses, use add_catalog_scene to download the selected scene into data/ and
   display the workspace-local copy. Tell the user that the asset is being
   downloaded and report its relative data/ path when complete.
3. Prefer the provided tools over run_python. Use run_python only for math or
   processing no tool covers (arbitrary NumPy/pandas, custom algorithms).
   run_python is sandboxed by default: basic stdlib (os, sys, pathlib, shutil,
   time, glob, csv) and the geospatial stack are importable, but subprocess,
   network, dynamic execution, and raw command calls (e.g. os.system) are
   rejected — do file I/O through read_file/write_file (or the raster/vector
   tools), which are already confined to the workspace. If the user enables
   "dangerous mode" in the UI, these restrictions are lifted.
   To look up an API signature/docstring/members, call python_help (e.g.
   python_help("rasterio.warp.reproject") or python_help("ws")) — never probe
   with dir()/__doc__/inspect inside run_python.
   run_python output is truncated to the first ~30 lines. For large output use
   inspect_output(start, count) to page through lines, or query_output(query)
   to extract a JSON/XML sub-value (jq-like keys, or an XPath-lite tag path).
4. Downloadable external file assets (especially plugin imagery/COGs) must
   be downloaded into data/ before they are used on the map or in analysis.
   Remote XYZ/WMS/basemap services may remain remote. Use download for one
   file or download_files for several independent files; the UI shows one
   progress cell per download. The batch tool returns one result per input,
   including successful paths and any individual error, so continue using
   successful files when one sibling fails. Tell the user when a download
   starts and where each completed file lives. Every path you pass must stay
   inside the workspace (relative paths resolve under the root). Read from
   data/, write under results/.
5. To show a raster on the map: inspect with raster_info, stretch with rescale,
   convert with to_cog, then add_raster(results/<name>.tif). Colormap "gray" for
   radar/SAR, "terrain" for elevation.
6. Vector data: read_vector, process, write results/*.geojson, then
   add_geojson or add_vector_to_map.
7. Sentinel-1 GRD (.SAFE): the imagery is <safe>/measurement/*-vv.tiff and
   *-vh.tiff. Use find_files to locate them, raster_info to inspect, rescale
   (percentile stretch) + to_cog, then add_raster. The annotation/*.xml files
   are large metadata — if you need them, read a slice with read_file using
   offset/limit instead of the whole file.
8. After changing the map, call describe_map to confirm state.
9. To focus the map on data you just added, call `fit_bounds` with the `bounds`
   from `raster_info` or `read_vector`. The embedded map bridge has no scripting
   RPC, so `zoom_to_layer`, `to_image`, `identify`, `fly_to`, and `fit_bounds`'s
   RPC siblings are unavailable — use `fit_bounds`/`set_view`/`describe_map` instead.
10. Report concisely what you did and where outputs live (relative paths).

Runtime environment (use the provided tools — never read installed-package or
GeoLibre source to discover capabilities):
- ``run_python`` exposes: rasterio, rioxarray, numpy, geopandas, pandas,
  shapely, pyproj, xarray. NOT installed: osgeo (gdal) and scipy.
- Valid ``colormap``/``palette`` names come from ``list_colormaps()`` (e.g.
  viridis, plasma, inferno, magma, cividis, turbo, blues, greens, reds,
  grays, gray, terrain). Use ``"gray"`` for SAR/radar, ``"terrain"`` for
  elevation, ``"blues"`` for water.
"""


_agent: "Agent | None" = None
_plan_store: "InMemoryPlanStore | None" = None

# Keep only routing, interaction, and basic orientation tools in every model
# request. Pydantic AI's ToolSearch capability automatically exposes the other
# tools on demand (native search where supported, local search elsewhere).
_CORE_TOOLS = frozenset(
    {
        "discover_capabilities",
        "request_user_input",
        "describe_geolibre_bridge",
        "describe_map",
        "list_files",
        "find_files",
    }
)


def resolve_model(model: str):
    """Resolve a model string to a ``Model`` instance, honoring a custom endpoint.

    When ``OPENAI_BASE_URL`` is set and ``model`` is an ``openai:`` model, the
    model is built with an ``OpenAIProvider`` bound to that endpoint (for Azure
    OpenAI, LiteLLM, vLLM, OpenRouter, and other OpenAI-compatible servers).
    Otherwise the string is returned unchanged (pydantic-ai infers the provider).
    """
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if not base_url or not model.startswith("openai:"):
        return model

    # OPENAI_BASE_URL is a base URL: pydantic-ai appends /chat/completions
    # itself. Tolerate a user-supplied full endpoint so a trailing
    # /chat/completions doesn't double-suffix into a 404.
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")].rstrip("/")

    from pydantic_ai.models import infer_model, infer_provider
    from pydantic_ai.providers.openai import OpenAIProvider

    def _factory(provider_name: str):
        if provider_name == "openai":
            return OpenAIProvider(base_url=base_url)
        return infer_provider(provider_name)

    return infer_model(model, provider_factory=_factory)


class ToolErrorFeedback(AbstractCapability[GeoContext]):
    """Convert a raised tool exception into a model-visible failure.

    By default pydantic-ai aborts the whole run when a tool raises. Geo-AI's
    tools raise ordinary exceptions for recoverable conditions (a bounds value
    outside the Web Mercator range, a file too large to read whole, a missing
    column), so we surface those as a ``ToolFailed`` result instead. The model
    sees the error and adapts in the same run — mirroring how ``run_python``
    returns a traceback string rather than raising.
    """

    async def on_tool_execute_error(self, ctx, *, call, tool_def, args, error):
        raise ToolFailed(f"{type(error).__name__}: {error}")




def build_agent(ctx: GeoContext, model: str) -> Agent:
    """Build a pydantic-ai ``Agent`` bound to ``ctx`` with every skill tool.

    The tools read the active context through the process-wide holder set here
    (marimo's chat adapter invokes the agent without injecting ``deps``), so the
    shared live map and workspace are always reachable. The built agent is also
    stored so callbacks can reach the latest instance without a stale closure.
    """
    global _agent, _plan_store
    set_context(ctx)
    _plan_store = InMemoryPlanStore()
    agent = Agent(
        resolve_model(model),
        system_prompt=SYSTEM_PROMPT,
        output_type=[str, DeferredToolRequests],
        capabilities=[
            ReinjectSystemPrompt(),
            Planning(store=_plan_store),
            ToolErrorFeedback(),
            TieredCompaction(
                tiers=[
                    ClearToolResults(max_tokens=1, keep_pairs=3),
                    SummarizingCompaction(max_messages=1, keep_messages=20),
                ],
                target_fraction=0.5,
            ),
        ],
    )
    for tool in ALL_TOOLS:
        agent.tool_plain(tool, defer_loading=tool.__name__ not in _CORE_TOOLS)

    _agent = agent
    return agent


def current_agent() -> Agent:
    """Return the most recently built agent, raising if none exists."""
    if _agent is None:
        raise RuntimeError("agent not built; call build_agent(ctx, model) first")
    return _agent


def current_plan_store() -> "InMemoryPlanStore | None":
    """Return the most recently built agent's plan store, if any."""
    return _plan_store
