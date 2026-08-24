"""Agent construction: system prompt + tool registration."""

from __future__ import annotations

import os

from pydantic_ai import Agent

from .context import GeoContext, set_context
from .skills import ALL_TOOLS

SYSTEM_PROMPT = """You are GeoAI, a geospatial-analysis agent in a Geo-AI web workspace.
You control a live GeoLibre map (visible to the user) and a workspace folder.

Workspace layout — all tool paths are relative to the workspace root:
- data/     user inputs (downloads, dropped files). Read here.
- results/  your outputs (GeoTIFF/COG, GeoJSON, tables). Write here.
- maps/     saved .geolibre.json projects.

Rules:
1. Prefer the provided tools over run_python. Use run_python only for math or
   processing no tool covers (arbitrary NumPy/pandas, custom algorithms).
2. Every path you pass must stay inside the workspace (relative paths resolve
   under the root). Read from data/, write under results/.
3. To show a raster on the map: inspect with raster_info, stretch with rescale,
   convert with to_cog, then add_raster(results/<name>.tif). Colormap "gray" for
   radar/SAR, "terrain" for elevation.
4. Vector data: read_vector, process, write results/*.geojson, then
   add_geojson or add_vector_to_map.
5. Sentinel-1 GRD (.SAFE): the imagery is <safe>/measurement/*-vv.tiff and
   *-vh.tiff. Use find_files to locate them, raster_info to inspect, rescale
   (percentile stretch) + to_cog, then add_raster. The annotation/*.xml files
   are large metadata — if you need them, read a slice with read_file using
   offset/limit instead of the whole file.
6. After changing the map, call describe_map to confirm state.
7. To focus the map on data you just added, call `fit_bounds` with the `bounds`
   from `raster_info` or `read_vector`. The embedded map bridge has no scripting
   RPC, so `zoom_to_layer`, `to_image`, `identify`, `fly_to`, and `fit_bounds`'s
   RPC siblings are unavailable — use `fit_bounds`/`set_view`/`describe_map` instead.
8. Report concisely what you did and where outputs live (relative paths).
"""


_agent: "Agent | None" = None
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

    from pydantic_ai.models import infer_model, infer_provider
    from pydantic_ai.providers.openai import OpenAIProvider

    def _factory(provider_name: str):
        if provider_name == "openai":
            return OpenAIProvider(base_url=base_url)
        return infer_provider(provider_name)

    return infer_model(model, provider_factory=_factory)


def build_agent(ctx: GeoContext, model: str) -> Agent:
    """Build a pydantic-ai ``Agent`` bound to ``ctx`` with every skill tool.

    The tools read the active context through the process-wide holder set here
    (marimo's chat adapter invokes the agent without injecting ``deps``), so the
    shared live map and workspace are always reachable. The built agent is also
    stored so callbacks can reach the latest instance without a stale closure.
    """
    global _agent
    set_context(ctx)
    agent = Agent(resolve_model(model), system_prompt=SYSTEM_PROMPT)
    for tool in ALL_TOOLS:
        agent.tool_plain(tool)

    @agent.system_prompt(dynamic=True)
    def _data_context(_ctx):  # noqa: ARG001 - context read via process-wide holder
        from .context import current

        files = current().workspace.list_files("data")
        if not files:
            return "The workspace data/ folder is currently empty."
        listing = "\n".join(f"- {f}" for f in files)
        return (
            "Files currently available in the workspace data/ folder "
            "(imported inputs the user may refer to):\n" + listing
        )

    _agent = agent
    return agent


def current_agent() -> Agent:
    """Return the most recently built agent, raising if none exists."""
    if _agent is None:
        raise RuntimeError("agent not built; call build_agent(ctx, model) first")
    return _agent
