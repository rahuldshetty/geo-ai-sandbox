"""Geo-AI harness: a geospatial-analysis agent for marimo notebooks."""

from .agent import SYSTEM_PROMPT, build_agent, current_agent
from .config import (
    DEFAULT_MODEL,
    list_workspaces,
    load_env,
    model_from_env,
    resolve_workspace_name,
    workspace_root,
)
from .context import GeoContext, current, set_context
from .map_view import create_map, persist_map
from .workspace import Workspace, WorkspaceError

__all__ = [
    "DEFAULT_MODEL",
    "GeoContext",
    "SYSTEM_PROMPT",
    "Workspace",
    "WorkspaceError",
    "build_agent",
    "create_map",
    "current",
    "current_agent",
    "list_workspaces",
    "load_env",
    "model_from_env",
    "persist_map",
    "resolve_workspace_name",
    "set_context",
    "workspace_root",
]
