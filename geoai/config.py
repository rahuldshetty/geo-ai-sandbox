"""Configuration and workspace-name resolution for the Geo-AI harness."""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_MODEL = "openai:gpt-4o"

# Repo root: <repo>/geoai/config.py -> <repo>/.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def model_from_env() -> str:
    """Return the agent model string, overridable via ``GEOAI_MODEL``."""
    return os.getenv("GEOAI_MODEL", DEFAULT_MODEL)


def max_retries() -> int:
    """Return the prompt-run retry cap, overridable via ``GEOAI_MAX_RETRIES``.

    Each run attempt covers a full agent invocation; transient failures (model
    API errors, an aborted run) retry this many times before the cell reports
    an error. Defaults to 5; values are clamped to at least 1.
    """
    raw = os.getenv("GEOAI_MAX_RETRIES", "5").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def resolve_workspace_name(override: str | None = None) -> str:
    """Resolve the active workspace name.

    Precedence: explicit ``override`` (a notebook's ``WORKSPACE_NAME``) →
    ``GEOAI_WORKSPACE`` env var → the notebook's ``__file__`` stem → ``"default"``.
    """
    if override and override.strip():
        return override.strip()
    env_name = os.getenv("GEOAI_WORKSPACE", "").strip()
    if env_name:
        return env_name
    main = sys.modules.get("__main__")
    file = getattr(main, "__file__", None) if main is not None else None
    if file:
        return Path(file).stem
    return "default"

def server_base_url() -> str:
    """Return the Geo-AI server's base URL for same-host file serving.

    The server binds ``127.0.0.1`` and serves workspace files from this origin
    (see ``/api/files/``), so the in-iframe map can fetch local rasters without
    the cross-origin failures of the geolibre static server's per-session tokens.
    The port mirrors the ``GEOAI_PORT`` env var used by ``geoai.server.run``.
    """
    port = os.getenv("GEOAI_PORT", "8000")
    return f"http://127.0.0.1:{port}/"


def workspace_root(name: str) -> Path:
    """Return the absolute workspace root for ``name``.

    Computed relative to the package location, never the process CWD.
    """
    return _PACKAGE_ROOT / "workspaces" / name


def list_workspaces() -> list[str]:
    """Return the names of existing workspace directories, sorted."""
    base = _PACKAGE_ROOT / "workspaces"
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir())


def load_env() -> None:
    """Load ``<repo>/.env`` into ``os.environ`` (no-op if dotenv is absent).

    Existing environment variables take precedence (dotenv default).
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(_PACKAGE_ROOT / ".env")
