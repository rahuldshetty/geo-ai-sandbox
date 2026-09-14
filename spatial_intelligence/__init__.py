"""Spatial Intelligence: a modular geospatial-analysis harness.

Layering (enforced by ``tests/spatial_intelligence/test_layering.py``):

* ``contracts`` depends on nothing else in the package.
* ``settings``, ``workspace``, ``map``, ``geo``, and ``pythonruntime`` are
  services: they know about the domain but not about tools, agents, or HTTP.
* ``tools`` declares and registers tools against those services.
* ``agent`` wires the registry into a pydantic-ai agent.
* ``session`` owns run state, jobs, and the notebook.
* ``server`` is the only layer that touches FastAPI.

The top level re-exports the pieces most callers need; importing it stays cheap
(no geolibre, rasterio, or FastAPI import).
"""

from importlib.metadata import PackageNotFoundError, version

from .contracts import (
    Effect,
    Job,
    JobState,
    ProgressEvent,
    ProgressSink,
    Reporter,
    SpatialIntelligenceError,
    ToolInputError,
    WorkspaceError,
    new_id,
)
from .tools import (
    ToolKind,
    ToolRegistry,
    ToolRuntime,
    bind,
    current_runtime,
    maybe_runtime,
    pack,
    tool,
)
from .tools.spec import ToolSpec
from .workspace import Workspace

__all__ = [
    "Effect",
    "Job",
    "JobState",
    "ProgressEvent",
    "ProgressSink",
    "Reporter",
    "SpatialIntelligenceError",
    "ToolInputError",
    "ToolKind",
    "ToolRegistry",
    "ToolRuntime",
    "ToolSpec",
    "Workspace",
    "WorkspaceError",
    "bind",
    "current_runtime",
    "maybe_runtime",
    "new_id",
    "pack",
    "tool",
]

# One source of truth for the version: the installed distribution (pyproject).
try:  # pragma: no cover - depends on how the tree is imported
    __version__ = version("spatial-intelligence")
except PackageNotFoundError:  # pragma: no cover - a checkout that is not installed
    __version__ = "0.0.0+source"
