"""JSON-file plan persistence for Pydantic AI Harness ``Planning``.

A drop-in ``PlanStore`` that stores the task plan as JSON instead of SQLite,
one file per workspace (``<workspace>/plan.json``). Implements the async CRUD
protocol; writes are atomic (temp file + ``os.replace``) and guarded by a lock.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from pydantic_ai_harness.planning import PlanItem


class JsonPlanStore:
    """Session-scoped JSON plan store.

    The file is a dict mapping session name -> list of ``PlanItem`` JSON
    objects, so one file can host several sessions. We use one file per
    workspace with the fixed session ``"default"``.
    """

    def __init__(self, path: str | Path, *, session: str = "default") -> None:
        self._path = Path(path)
        self._session = session
        self._lock = threading.Lock()

    # -- file I/O ---------------------------------------------------------

    def _read_sessions(self) -> dict[str, list[dict[str, Any]]]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _load_items(self) -> list[PlanItem]:
        raw = self._read_sessions().get(self._session, [])
        items: list[PlanItem] = []
        for item in raw:
            try:
                items.append(PlanItem.model_validate(item))
            except Exception:  # noqa: BLE001 - skip a malformed row, keep the rest
                continue
        return items

    def _save_items(self, items: list[PlanItem]) -> None:
        sessions = self._read_sessions()
        sessions[self._session] = [item.model_dump(mode="json") for item in items]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(sessions, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self._path)

    # -- PlanStore protocol -----------------------------------------------

    async def get_items(self) -> list[PlanItem]:
        with self._lock:
            return self._load_items()

    async def set_items(self, items: list[PlanItem]) -> None:
        with self._lock:
            self._save_items(items)

    async def get_item(self, item_id: str) -> PlanItem | None:
        with self._lock:
            return next((i for i in self._load_items() if i.id == item_id), None)

    async def add_item(self, item: PlanItem) -> PlanItem:
        with self._lock:
            items = self._load_items()
            if any(i.id == item.id for i in items):
                raise ValueError(f"a step with id {item.id!r} is already in this plan")
            items.append(item)
            self._save_items(items)
            return item

    async def update_item(
        self,
        item_id: str,
        *,
        content: str | None = None,
        status: Any = None,
        active_form: str | None = None,
        parent_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> PlanItem | None:
        with self._lock:
            items = self._load_items()
            for idx, item in enumerate(items):
                if item.id != item_id:
                    continue
                updates: dict[str, Any] = {}
                if content is not None:
                    updates["content"] = content
                if status is not None:
                    updates["status"] = status
                if active_form is not None:
                    updates["active_form"] = active_form
                if parent_id is not None:
                    updates["parent_id"] = parent_id
                if depends_on is not None:
                    updates["depends_on"] = depends_on
                updated = item.model_copy(update=updates)
                items[idx] = updated
                self._save_items(items)
                return updated
            return None

    async def remove_item(self, item_id: str) -> bool:
        with self._lock:
            items = self._load_items()
            for idx, item in enumerate(items):
                if item.id == item_id:
                    items.pop(idx)
                    self._save_items(items)
                    return True
            return False
