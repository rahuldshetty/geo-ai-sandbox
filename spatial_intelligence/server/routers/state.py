"""App-level state, settings, and the GeoLibre bridge handshake."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ...map.bridge import bridge_info, update_bridge
from ..deps import app_state

router = APIRouter(tags=["state"])


class UpdateSettings(BaseModel):
    model: str | None = None
    theme: str | None = None
    dangerous_mode: bool | None = None
    max_retries: int | None = None
    record_agent_steps: bool | None = None


class GeoLibreBridgeHandshake(BaseModel):
    version: str | None = None
    methods: list[str] = Field(default_factory=list)


@router.get("/api/state")
def api_state() -> dict:
    """Return the full snapshot the browser renders from."""
    return app_state().snapshot()


@router.get("/api/settings")
def api_get_settings() -> dict:
    """Return the persisted user settings."""
    return dict(app_state().settings)


@router.put("/api/settings")
def api_update_settings(body: UpdateSettings) -> dict:
    """Merge a settings patch, persist it, and broadcast the result."""
    patch = {key: value for key, value in body.model_dump().items() if value is not None}
    return app_state().update_settings(patch)


@router.post("/api/geolibre/bridge")
def api_geolibre_bridge(body: GeoLibreBridgeHandshake) -> dict:
    """Record the capabilities the embedded GeoLibre iframe reports."""
    return update_bridge(body.version, body.methods)


@router.get("/api/geolibre/bridge")
def api_get_geolibre_bridge() -> dict:
    """Return the last handshake from the embedded map."""
    return bridge_info()
