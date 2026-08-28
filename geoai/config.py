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


def keep_messages() -> int:
    """Return how many recent prior messages to replay into a new prompt cell.

    ``GEOAI_KEEP_MESSAGES`` (default 24). ``0`` disables cross-cell replay.
    """
    raw = os.getenv("GEOAI_KEEP_MESSAGES", "24").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 24
    return max(0, value)


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
