"""App-level user settings persisted to ``<repo>/settings.json``.

Settings are runtime-editable (the Settings dialog): model and UI theme.
Environment variables seed the first-run defaults; once a value is
written to the file it takes precedence.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import DEFAULT_MODEL, max_retries, model_from_env

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

_THEMES = ("light", "dark")


def _defaults() -> dict:
    return {
        "model": model_from_env() or DEFAULT_MODEL,
        "theme": "light",
        "dangerous_mode": False,
        "max_retries": max_retries(),
    }

def load_settings() -> dict:
    """Return the merged settings (env defaults overlaid by the file)."""
    settings = _defaults()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        if isinstance(data, dict):
            for key in settings:
                value = data.get(key)
                if value not in (None, ""):
                    settings[key] = value
    return _normalize(settings)


def save_settings(settings: dict) -> dict:
    """Normalize and persist ``settings``; return the normalized dict."""
    normalized = _normalize(settings)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return normalized


def _normalize(settings: dict) -> dict:
    defaults = _defaults()
    model = (settings.get("model") or "").strip() or defaults["model"]
    theme = settings.get("theme") if settings.get("theme") in _THEMES else "light"
    try:
        retries = int(settings.get("max_retries", defaults["max_retries"]))
    except (TypeError, ValueError):
        retries = defaults["max_retries"]
    return {
        "model": model,
        "theme": theme,
        "dangerous_mode": bool(settings.get("dangerous_mode", False)),
        "max_retries": max(1, retries),
    }
