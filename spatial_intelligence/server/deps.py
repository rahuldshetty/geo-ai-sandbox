"""The process-wide session, created on first use.

Kept behind a function so importing a router (or the app) never constructs the
live map, opens a workspace, or starts the run worker — tests and tooling can
import the server package without side effects.
"""

from __future__ import annotations

from functools import lru_cache

from ..session.app_state import AppState
from ..settings.env import load_env


@lru_cache(maxsize=1)
def app_state() -> AppState:
    """Return the process-wide :class:`AppState`, creating it once."""
    load_env()
    return AppState()
