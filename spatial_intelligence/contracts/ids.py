"""Identifier helpers shared by cells, runs, jobs, and interactions."""

from __future__ import annotations

import uuid


def new_id() -> str:
    """Return a fresh opaque identifier (32 hex chars)."""
    return uuid.uuid4().hex
