"""Geo-AI server: FastAPI backend + custom web UI (no marimo)."""

from __future__ import annotations

import os

from ..config import load_env
from .app import app
from .state import state

__all__ = ["app", "run", "state"]


def run() -> None:
    load_env()
    port = int(os.getenv("GEOAI_PORT", "8000"))
    import uvicorn

    uvicorn.run("geoai.server.app:app", host="127.0.0.1", port=port, reload=False)
