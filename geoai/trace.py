"""Agent trace persistence: append-only JSONL under ``<workspace>/traces/``.

Each prompt-cell run writes one JSONL file named ``<cell_id>.jsonl``. A run
writes four record kinds, one JSON object per line:

    {"type": "run",      "ts", "cell_id", "run_id", "model", "prompt"}
    {"type": "step",     "step": {...}}                 # one UI trace step
    {"type": "result",   "ts", "status", "output", "error", "usage", "conversation_id"}
    {"type": "messages", "messages": [...]}             # new_messages, JSON-able

Re-running a cell truncates its file: a cell re-run replaces its own trace.
Reads are tolerant of partial writes (crash mid-run leaves the earlier records
usable).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage
from pydantic_core import to_jsonable_python


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_run(
    path: Path,
    *,
    cell_id: str,
    run_id: str,
    model: str,
    prompt: str,
    ts: str | None = None,
) -> None:
    """Start a run, truncating any prior trace for the same cell."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    _append(
        path,
        {
            "type": "run",
            "ts": ts or now_iso(),
            "cell_id": cell_id,
            "run_id": run_id,
            "model": model,
            "prompt": prompt,
        },
    )


def append_step(path: Path, step: dict[str, Any]) -> None:
    """Append one UI trace step (already JSON-safe)."""
    _append(path, {"type": "step", "step": step})


def append_result(
    path: Path,
    *,
    status: str,
    output: str | None = None,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    ts: str | None = None,
) -> None:
    _append(
        path,
        {
            "type": "result",
            "ts": ts or now_iso(),
            "status": status,
            "output": output,
            "error": error,
            "usage": usage,
            "conversation_id": conversation_id,
        },
    )


def append_messages(path: Path, messages: list[ModelMessage]) -> None:
    """Persist the run's ``new_messages`` (conversation continuity payload)."""
    _append(path, {"type": "messages", "messages": to_jsonable_python(messages)})


def usage_to_dict(usage: Any) -> dict[str, Any]:
    """Flatten a ``RunUsage`` into a JSON-serializable dict."""
    details = getattr(usage, "details", None) or {}
    cache_read = int(getattr(usage, "cache_read_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_write_tokens", 0) or 0)
    if not cache_read:
        cache_read = int(details.get("prompt_cache_hit_tokens", 0) or 0)
    if not cache_write:
        cache_write = int(details.get("prompt_cache_miss_tokens", 0) or 0)
    return {
        "requests": int(getattr(usage, "requests", 0) or 0),
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        "tool_calls": int(getattr(usage, "tool_calls", 0) or 0),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost": _cost(usage),
    }


def _cost(usage: Any) -> float | None:
    cost = getattr(usage, "cost", None)
    if cost is None:
        return None
    return float(cost)


def _iter_records(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def read_trace(path: Path, *, include_messages: bool = True) -> dict[str, Any]:
    """Load a trace file into a display-oriented dict (empty defaults when absent).

    Returns keys: ``steps`` (UI steps), ``usage``, ``messages`` (ModelMessage
    objects, only when ``include_messages``), ``run_id``, ``conversation_id``,
    ``model``, ``prompt``, ``status``, ``output``, ``error``.
    """
    result: dict[str, Any] = {
        "steps": [],
        "usage": None,
        "messages": [],
        "run_id": None,
        "conversation_id": None,
        "model": None,
        "prompt": None,
        "status": None,
        "output": None,
        "error": None,
    }
    for rec in _iter_records(path):
        kind = rec.get("type")
        if kind == "run":
            result["run_id"] = rec.get("run_id")
            result["model"] = rec.get("model")
            result["prompt"] = rec.get("prompt")
        elif kind == "step":
            result["steps"].append(rec.get("step"))
        elif kind == "result":
            result["status"] = rec.get("status")
            result["output"] = rec.get("output")
            result["error"] = rec.get("error")
            result["usage"] = rec.get("usage")
            result["conversation_id"] = rec.get("conversation_id")
        elif kind == "messages" and include_messages:
            result["messages"] = _messages_from_jsonable(rec.get("messages") or [])
    return result


def read_messages(path: Path) -> list[ModelMessage]:
    """Return the ``new_messages`` persisted in ``path`` ([] when absent)."""
    for rec in _iter_records(path):
        if rec.get("type") == "messages":
            return _messages_from_jsonable(rec.get("messages") or [])
    return []


def _messages_from_jsonable(records: list[Any]) -> list[ModelMessage]:
    try:
        return ModelMessagesTypeAdapter.validate_python(records)
    except Exception:  # noqa: BLE001 - never let a corrupt trace block a run
        return []
