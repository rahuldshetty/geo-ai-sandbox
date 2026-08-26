"""App-level user settings persisted to ``<repo>/settings.json``.

Settings are runtime-editable (the Settings dialog): model, token budget, and
UI theme. Environment variables seed the first-run defaults; once a value is
written to the file it takes precedence.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import DEFAULT_MODEL, model_from_env

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

_THEMES = ("light", "dark")
_DEFAULT_BUDGET = 32000


def _defaults() -> dict:
    return {
        "model": model_from_env() or DEFAULT_MODEL,
        "max_history_tokens": _DEFAULT_BUDGET,
        "theme": "light",
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
    try:
        budget = int(settings.get("max_history_tokens", _DEFAULT_BUDGET))
    except (TypeError, ValueError):
        budget = _DEFAULT_BUDGET
    theme = settings.get("theme") if settings.get("theme") in _THEMES else "light"
    return {
        "model": model,
        "max_history_tokens": max(0, budget),
        "theme": theme,
    }
