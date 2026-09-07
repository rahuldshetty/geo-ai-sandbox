"""Geo-AI server: FastAPI backend + custom web UI (no marimo)."""

from __future__ import annotations

import os

from ..config import load_env
from .app import app
from .state import state

__all__ = ["app", "run", "state"]


def _open_browser(url: str) -> None:
    """Open the UI once the server has had a moment to come up.

    Suppressed with ``GEOAI_NO_BROWSER=1`` (used by packaging and tests).
    """
    if os.getenv("GEOAI_NO_BROWSER", "").strip().lower() in {"1", "true", "yes"}:
        return
    import threading
    import webbrowser

    threading.Timer(1.0, webbrowser.open, (url,)).start()


def run() -> None:
    load_env()
    port = int(os.getenv("GEOAI_PORT", "8000"))
    _open_browser(f"http://127.0.0.1:{port}/")
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
