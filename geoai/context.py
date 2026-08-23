"""Shared agent context: the live map, the workspace, and a reactivity trigger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from geolibre import Map

from .workspace import Workspace


@dataclass
class GeoContext:
    """State the tools operate on.

    Attributes:
        map: The shared live GeoLibre map, or ``None`` in headless tests.
        workspace: The active workspace.
        version: A zero-arg callable that bumps marimo reactivity (the Files
            tab's ``mo.state`` setter), or ``None`` when reactivity is unused.
    """

    map: "Map | None"
    workspace: Workspace
    version: Any = None

    def notify(self) -> None:
        """Trigger marimo reactivity after a file write (no-op when absent)."""
        if callable(self.version):
            self.version()


_current: "GeoContext | None" = None


def set_context(ctx: "GeoContext | None") -> None:
    """Bind the active context (used by ``build_agent`` and headless tests)."""
    global _current
    _current = ctx


def current() -> "GeoContext":
    """Return the active context, raising if none is bound."""
    if _current is None:
        raise RuntimeError("GeoAI context not initialized; call build_agent(ctx, model) first")
    return _current
