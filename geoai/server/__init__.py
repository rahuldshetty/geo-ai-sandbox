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


def _open_browser(url: str) -> None:
    """Open the UI once the server has had a moment to come up.

    Suppressed with ``GEOAI_NO_BROWSER=1`` (used by packaging and tests).
    """
    if os.getenv("GEOAI_NO_BROWSER", "").strip().lower() in {"1", "true", "yes"}:
        return
    import threading
    import webbrowser

    threading.Timer(1.0, webbrowser.open, (url,)).start()


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def run() -> None:
    port = int(os.getenv("GEOAI_PORT", "8000"))
    if _port_in_use(port):
        raise SystemExit(
            f"Geo-AI: port {port} is already in use; "
            "set GEOAI_PORT to another port and start again."
        )
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
