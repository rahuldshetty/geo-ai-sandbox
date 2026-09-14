"""Capability discovery: the compact index of what this build can actually do."""

from __future__ import annotations

from typing import Any

from ... import discovery
from ...contracts.effects import Effect
from ..runtime import ToolRuntime
from ..spec import pack, tool

#: Service key under which the session publishes its tool registry.
REGISTRY_SERVICE = "tools.registry"


@pack(category="capability", effects=frozenset({Effect.READ}))
class CapabilityPack:
    """Catalog search backed by the session's tool registry when there is one."""

    def __init__(self, runtime: ToolRuntime) -> None:
        self._rt = runtime

    @tool(core=True)
    def discover_capabilities(self, goal: str, limit: int = 5) -> list[dict]:
        """Find compact backend and GeoLibre capabilities relevant to a task.

        Use this before improvising with ``run_python`` or claiming an integration
        is unavailable. Results identify executable backend tools and GeoLibre
        plugins that currently require an interactive handoff.
        """
        return discovery.discover(self._registry(), goal, limit)

    def _registry(self) -> Any | None:
        """Return the registry the session published, if it has done so yet.

        The pack is constructed with the runtime alone, so the registry -- which
        is assembled by the session, after the packs it describes -- arrives
        through the service bag under ``'tools.registry'``. Reading (never
        creating) it keeps a session that has not wired one up working: the
        catalog is still ranked, just without tool summaries.
        """
        return self._rt.services.get(REGISTRY_SERVICE)


__all__ = ["CapabilityPack"]
