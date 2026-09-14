"""The scene cache: what one workspace's catalog searches remembered.

A search result carries a stable ``scene_key``; the download and map tools take
that key instead of a URL, so the cache is what makes "search, then add" a
two-step flow the model cannot get wrong. Scenes are held in memory for the
session *and* persisted to ``traces/catalog-scenes.json``, because the map tool
may run in a later run than the search that produced the key.

The cache is an object, not a module global: a session owns exactly one, and a
test (or a second workspace in the same process) cannot see another's scenes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock

from ...workspace import Workspace

#: Name of the persisted cache inside ``traces/``.
CACHE_FILENAME = "catalog-scenes.json"
#: Most scenes kept on disk; older ones are dropped.
MAX_CACHED_SCENES = 500


def scene_key(provider: str, item_id: str, asset_url: str) -> str:
    """Return the stable key for one catalog scene.

    The key is derived from the provider, the item id, and the asset URL, so the
    same scene keeps its identity across searches while a re-published asset
    gets a new one.
    """
    digest = hashlib.sha256(asset_url.encode("utf-8")).hexdigest()[:12]
    return f"{provider}:{item_id}:{digest}"


class SceneCache:
    """The scenes one workspace's catalog searches have produced.

    Reads fall back to the persisted file, so a ``scene_key`` handed back to a
    later tool call (or a later run) still resolves. Writes are best-effort:
    a cache that cannot be persisted must never fail a search that succeeded.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._lock = Lock()
        self._scenes: dict[str, dict] = {}

    @property
    def path(self) -> Path:
        """The file the cache persists to."""
        return self._workspace.traces / CACHE_FILENAME

    def remember(self, scene: dict) -> dict:
        """Store ``scene`` in memory and on disk; return it unchanged."""
        with self._lock:
            self._scenes[scene["scene_key"]] = dict(scene)
            try:
                stored = self._read_file()
                stored[scene["scene_key"]] = scene
                # Keep the cache bounded while preserving the newest inserted keys.
                stored = dict(list(stored.items())[-MAX_CACHED_SCENES:])
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(stored, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except (OSError, ValueError, json.JSONDecodeError):
                # Search results are still usable in this process if cache
                # persistence is unavailable.
                pass
        return scene

    def get(self, scene_key: str) -> dict:
        """Return a remembered scene, or ``{}`` when the key is unknown."""
        with self._lock:
            scene = self._scenes.get(scene_key)
            if scene:
                return dict(scene)
            try:
                stored = self._read_file()
                scene = stored.get(scene_key)
                if isinstance(scene, dict):
                    self._scenes[scene_key] = dict(scene)
                    return dict(scene)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return {}

    # -- internals -------------------------------------------------------

    def _read_file(self) -> dict:
        """Return the persisted scenes, or ``{}`` when the file is unusable."""
        if not self.path.exists():
            return {}
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        return stored if isinstance(stored, dict) else {}


__all__ = ["CACHE_FILENAME", "MAX_CACHED_SCENES", "SceneCache", "scene_key"]
