"""Configuration and workspace-name resolution for the Geo-AI harness."""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_MODEL = "openai:gpt-4o"

# Package location: <root>/geoai/config.py -> <root>/.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def app_root() -> Path:
    """Return the data root for ``workspaces/``, ``settings.json``, ``.env``.

    ``GEOAI_HOME`` wins when set. Otherwise a dev checkout (a ``.git`` dir or
    an existing ``workspaces/`` next to the package) keeps data beside the
    source; anything else (a pip-installed or frozen app) falls back to the
    XDG data home at ``~/.local/share/geo-ai``.
    """
    env = os.getenv("GEOAI_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if (_PACKAGE_ROOT / ".git").exists() or (_PACKAGE_ROOT / "workspaces").is_dir():
        return _PACKAGE_ROOT
    xdg = os.getenv("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "geo-ai"


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
    """Return the absolute workspace root for ``name`` (under ``app_root()``).

    Computed from the data root, never the process CWD.
    """
    return app_root() / "workspaces" / name


def list_workspaces() -> list[str]:
    """Return the names of existing workspace directories, sorted."""
    base = app_root() / "workspaces"
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir())


def load_env() -> None:
    """Load ``<app_root>/.env`` into ``os.environ`` (no-op if dotenv is absent).

    Existing environment variables take precedence (dotenv default).
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(app_root() / ".env")
