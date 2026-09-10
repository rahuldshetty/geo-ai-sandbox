"""Geo-AI harness: a geospatial-analysis agent for notebook workspaces."""

from importlib import import_module

from .config import (
    DEFAULT_MODEL,
    list_workspaces,
    load_env,
    model_from_env,
    resolve_workspace_name,
    workspace_root,
)
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

_LAZY_EXPORTS = {
    "SYSTEM_PROMPT": (".agent", "SYSTEM_PROMPT"),
    "build_agent": (".agent", "build_agent"),
    "current_agent": (".agent", "current_agent"),
    "GeoContext": (".context", "GeoContext"),
    "current": (".context", "current"),
    "set_context": (".context", "set_context"),
    "create_map": (".map_view", "create_map"),
    "persist_map": (".map_view", "persist_map"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value
