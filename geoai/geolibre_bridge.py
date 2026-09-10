"""Runtime description of the GeoLibre iframe's versioned embed bridge."""

from __future__ import annotations

import threading

_lock = threading.Lock()
_info: dict = {
    "connected": False,
    "version": None,
    "protocol_version": 1,
    "methods": [],
}


def update_bridge(version: str | None, methods: list[str] | None = None) -> dict:
    """Record capabilities reported by the currently connected iframe."""
    with _lock:
        _info.update(
            {
                "connected": True,
                "version": version,
                "protocol_version": 1,
                "methods": sorted(set(methods or [])),
            }
        )
        return dict(_info)


def bridge_info() -> dict:
    """Return the last capability handshake from the GeoLibre iframe."""
    with _lock:
        return dict(_info)

