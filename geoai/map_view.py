"""Map view: create the shared live GeoLibre map and persist it."""

from __future__ import annotations

from geolibre import Map

from .workspace import Workspace

_SNAPSHOT = "current.geolibre.json"


def create_map(ws: Workspace, height: str = "80vh") -> Map:
    """Create the shared map, restoring the last saved snapshot if present."""
    m = Map(center=(0, 0), zoom=2, height=height, layout="embed", theme="light")
    snap = ws.maps / _SNAPSHOT
    if snap.exists():
        m.load_project(snap)
    return m


def persist_map(m: Map, ws: Workspace) -> None:
    """Save the current project to the workspace snapshot and bump it."""
    m.save_project(str(ws.maps / _SNAPSHOT))
    ws.bump()
