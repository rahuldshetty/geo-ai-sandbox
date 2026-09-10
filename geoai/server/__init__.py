"""Geo-AI server: FastAPI backend + custom web UI (no marimo)."""

from __future__ import annotations

import os
from importlib import import_module

from ..config import load_env

__all__ = ["app", "run", "state"]

# Configuration must be present before importing ``app`` creates the process-wide
# AppState singleton. Keeping app/state lazy also lets lightweight modules such
# as notebook serialization be imported without starting GeoLibre.
load_env()


def __getattr__(name: str):
    if name == "app":
        return import_module(".app", __name__).app
    if name == "state":
        return import_module(".state", __name__).state
    raise AttributeError(name)


def run() -> None:
    port = int(os.getenv("GEOAI_PORT", "8000"))
    import uvicorn

    # An SSE client holds a long-lived connection that never completes on its
    # own, so a single Ctrl+C would otherwise wait forever for it to close.
    # Bound the grace period so shutdown force-cancels the stream and exits.
    uvicorn.run(
        "geoai.server.app:app",
        host="127.0.0.1",
        port=port,
        reload=False,
        timeout_graceful_shutdown=3,
    )
