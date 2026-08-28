"""Process-wide server state: the live map, the active workspace, and cells.

A single ``AppState`` singleton owns the GeoLibre map and notebook cells, guarded
by one ``threading.RLock``. FastAPI sync endpoints run in a threadpool and wrap
every mutation in ``state.lock``; cell runs hold the lock for their full
duration, so workspace open/close/save blocks until the current run finishes
(acceptable for a single-user desktop app).
"""

from __future__ import annotations

import asyncio
import queue
import threading
import traceback
import uuid
from pathlib import Path
from urllib.parse import urlparse

from geolibre import Map
from geolibre import project as _project
from pydantic_ai import CancellationToken, RunCancelled


from .. import trace
from ..agent import build_agent, current_agent, current_plan_store
from ..config import list_workspaces, load_env, workspace_root
from ..context import GeoContext, set_context
from ..map_view import persist_map
from ..settings import load_settings, save_settings
from ..skills.python_tools import get_last_output_text, run_python, set_dangerous_mode
from ..skills.workspace_tools import download
from ..workspace import Workspace
from ..skills.map_tools import repoint_local_rasters
from .notebook import new_cell, read_nb, write_nb

_SNAPSHOT = "current.geolibre.json"
_RUNNABLE_KINDS = frozenset({"python", "prompt"})

_PLAN_TOOLS = frozenset(
    {"write_plan", "add_task", "update_task_status", "update_task_statuses", "remove_task"}
)


def _filename_from_url(url: str) -> str:
    """Derive a download filename from a URL path, with a safe fallback."""
    name = Path(urlparse(url).path).name
    return name or "download"


def _json_safe(value: object) -> object:
    """Coerce a tool arg/result into a JSON-serializable structure.

    Preserves dict/list shape (so the UI can pretty-print JSON and detect
    code-bearing tools) while falling back to ``str`` for anything exotic.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _event_to_step(event: object) -> dict | None:
    """Map one pydantic-ai stream event to a UI trace step (or ``None``)."""
    ek = getattr(event, "event_kind", None)
    if ek == "function_tool_call":
        part = event.part
        return {
            "type": "tool_call",
            "name": part.tool_name,
            "args": _json_safe(part.args),
            "tool_call_id": part.tool_call_id,
        }
    if ek == "function_tool_result":
        part = event.part
        return {
            "type": "tool_result",
            "name": getattr(part, "tool_name", None),
            "content": _json_safe(getattr(part, "content", None)),
            "tool_call_id": event.tool_call_id,
        }
    if ek == "part_start":
        p = event.part
        if getattr(p, "part_kind", None) in ("text", "thinking"):
            content = getattr(p, "content", "") or ""
            if content:
                return {"type": "text", "content": content}
    elif ek == "part_delta":
        d = event.delta
        if getattr(d, "part_delta_kind", None) in ("text", "thinking"):
            content = getattr(d, "content_delta", "") or ""
            if content:
                return {"type": "text_delta", "content": content}
    return None


def _stream_output(text: str) -> dict:
    return {
        "output_type": "stream",
        "name": "stdout",
        "text": text,
        "ename": None,
        "evalue": None,
        "traceback": None,
    }


def _error_output(ename: str, text: str) -> dict:
    return {
        "output_type": "error",
        "name": None,
        "text": None,
        "ename": ename,
        "evalue": text,
        "traceback": [text],
    }


class AppState:
    """Single-owner state for the running Geo-AI server."""
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings = load_settings()
        set_dangerous_mode(self.settings.get("dangerous_mode", False))
        self.model = self.settings["model"]
        self.map = Map(
            center=(0, 0), zoom=2, height="100%", layout="embed", theme="light"
        )
        self.active_name: str | None = None
        self.workspace: Workspace | None = None
        self.cells: list[dict] = []
        self._subscribers: list[queue.Queue] = []
        self._subscribers_lock = threading.Lock()
        self._run_q: queue.Queue = queue.Queue()
        self._run_tokens: dict[str, CancellationToken] = {}
        self._cancelled: set[str] = set()
        self._run_tokens_lock = threading.Lock()

        worker = threading.Thread(target=self._run_worker, name="geoai-run-worker", daemon=True)
        worker.start()

    # -- workspace lifecycle ---------------------------------------------

    def open_workspace(self, name: str) -> None:
        with self.lock:
            ws = Workspace(workspace_root(name)).create()
            self.cells = read_nb(ws.root / "notebook.ipynb")
            self._rehydrate_traces(ws)
            snap = ws.maps / _SNAPSHOT
            if snap.exists():
                self.map.load_project(snap)
            else:
                self.map.load_project(
                    _project.build_empty_project(center=(0, 0), zoom=2)
                )
            # Local raster layers are persisted with session-local URLs (the
            # geolibre static server binds a random port and a per-process token).
            # Re-register the files and re-point the layers so the in-iframe app
            # can fetch them again after a server restart.
            repoint_local_rasters(self.map, ws)
            ctx = GeoContext(map=self.map, workspace=ws, version=None)
            set_context(ctx)
            build_agent(ctx, self.model)
            self.active_name = name
            self.workspace = ws

    def new_workspace(self, name: str) -> None:
        with self.lock:
            Workspace(workspace_root(name)).create()
        self.open_workspace(name)

    def close_workspace(self) -> None:
        with self.lock:
            self.active_name = None
            self.workspace = None
            self.cells = []
            set_context(None)
            self.map.load_project(
                _project.build_empty_project(center=(0, 0), zoom=2)
            )

    def save_workspace(self) -> dict:
        with self.lock:
            if self.workspace is not None:
                write_nb(self.workspace.root / "notebook.ipynb", self.cells)
                persist_map(self.map, self.workspace)
            return {"ok": True}

    def update_settings(self, patch: dict) -> dict:
        """Merge ``patch`` into settings, persist, and rebuild the agent on change.

        Model changes require a rebuilt agent; theme and dangerous-mode changes
        do not. A rebuild failure (e.g. an invalid model string) rolls back.
        """
        with self.lock:
            old_model = self.settings.get("model")
            rebuild = False
            if "model" in patch and patch["model"]:
                new_model = patch["model"].strip()
                if new_model and new_model != old_model:
                    self.settings["model"] = new_model
                    self.model = new_model
                    rebuild = True
            if "theme" in patch and patch["theme"] in ("light", "dark"):
                self.settings["theme"] = patch["theme"]
            if "dangerous_mode" in patch and patch["dangerous_mode"] is not None:
                self.settings["dangerous_mode"] = bool(patch["dangerous_mode"])
            try:
                self.settings = save_settings(self.settings)
                if rebuild and self.workspace is not None:
                    ctx = GeoContext(map=self.map, workspace=self.workspace, version=None)
                    set_context(ctx)
                    build_agent(ctx, self.model)
            except Exception as exc:  # noqa: BLE001 - roll back and surface
                self.settings["model"] = old_model
                self.model = old_model
                self.settings = save_settings(self.settings)
                raise ValueError(f"could not apply settings: {exc}") from exc
            set_dangerous_mode(self.settings.get("dangerous_mode", False))
            return dict(self.settings)

    # -- traces ------------------------------------------------------------

    def _trace_path(self, cell_id: str) -> "Path | None":
        if self.workspace is None:
            return None
        return self.workspace.traces / f"{cell_id}.jsonl"

    def _rehydrate_traces(self, ws: Workspace) -> None:
        """Restore per-cell trace steps and token usage from ``traces/*.jsonl``."""
        for cell in self.cells:
            if cell.get("kind") != "prompt":
                continue
            loaded = trace.read_trace(ws.traces / f"{cell['id']}.jsonl", include_messages=False)
            if loaded["steps"]:
                cell["trace"] = loaded["steps"]
            if loaded["usage"] is not None:
                cell["usage"] = loaded["usage"]
            if loaded["run_id"]:
                cell["run_id"] = loaded["run_id"]
            if loaded["conversation_id"]:
                cell["conversation_id"] = loaded["conversation_id"]

    def _finish_trace(
        self,
        cell_id: str,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        usage: dict | None = None,
        conversation_id: str | None = None,
    ) -> None:
        path = self._trace_path(cell_id)
        if path is not None:
            trace.append_result(
                path,
                status=status,
                output=output,
                error=error,
                usage=usage,
                conversation_id=conversation_id,
            )

    # -- cell ops ----------------------------------------------------------

    def _find_cell(self, cell_id: str) -> dict:
        for cell in self.cells:
            if cell["id"] == cell_id:
                return cell
        raise KeyError(cell_id)

    def _save_cells(self) -> None:
        if self.workspace is not None:
            write_nb(self.workspace.root / "notebook.ipynb", self.cells)

    def add_cell(self, kind: str, source: str = "", index: int | None = None) -> dict:
        with self.lock:
            cell = new_cell(kind, source)
            if index is None or index < 0:
                self.cells.append(cell)
            else:
                self.cells.insert(min(index, len(self.cells)), cell)
            self._save_cells()
            return cell

    def update_cell(self, cell_id: str, source: str) -> dict:
        with self.lock:
            cell = self._find_cell(cell_id)
            cell["source"] = source
            self._save_cells()
            return cell

    def delete_cell(self, cell_id: str) -> None:
        with self.lock:
            cell = self._find_cell(cell_id)
            self.cells.remove(cell)
            self._save_cells()

    def move_cell(self, cell_id: str, index: int) -> None:
        with self.lock:
            cell = self._find_cell(cell_id)
            self.cells.remove(cell)
            self.cells.insert(min(max(index, 0), len(self.cells)), cell)
            self._save_cells()

    # -- run queue / worker ------------------------------------------------

    def run_cell(self, cell_id: str) -> None:
        with self.lock:
            cell = self._find_cell(cell_id)
            if cell["kind"] not in _RUNNABLE_KINDS:
                raise ValueError("markdown cells are not runnable")
            cell["status"] = "running"
            cell["outputs"] = []
            cell["trace"] = []
            cell["usage"] = None
            cell["run_id"] = None
            cell["conversation_id"] = None
            self.broadcast(
                "cell",
                {"id": cell_id, "status": "running", "trace": [], "usage": None},
            )
            self._run_q.put(cell_id)

    def run_all(self) -> None:
        with self.lock:
            for cell in self.cells:
                if cell["kind"] in _RUNNABLE_KINDS:
                    self._run_q.put(cell["id"])

    def _execute_cell(self, cell: dict) -> None:
        source = cell["source"]
        kind = cell["kind"]
        prev = cell.get("execution_count")
        cell["execution_count"] = (prev or 0) + 1

        if kind == "python":
            try:
                run_python(source)
                output = get_last_output_text()
            except Exception as exc:  # noqa: BLE001 - surface failures in the cell output
                output = "ERROR: " + str(exc)
            cell["trace"] = []
            if output.startswith("ERROR:"):
                cell["status"] = "error"
                cell["outputs"] = [_error_output("PythonError", output)]
            else:
                cell["status"] = "done"
                cell["outputs"] = [_stream_output(output)]
        else:  # prompt
            trace_steps: list[dict] = []
            result = self._run_prompt(cell["id"], source, trace_steps.append)
            cell["trace"] = trace_steps
            cell["usage"] = result.get("usage")
            cell["run_id"] = result.get("run_id")
            cell["conversation_id"] = result.get("conversation_id")
            if result.get("stopped"):
                cell["status"] = "stopped"
                cell["outputs"] = [_stream_output("Stopped.")]
            elif result.get("error") is not None:
                cell["status"] = "error"
                cell["outputs"] = [_error_output("AgentError", f"ERROR: {result['error']}")]
            else:
                cell["status"] = "done"
                cell["outputs"] = [_stream_output(result["output"])]
        self._save_cells()

    def stop_cell(self, cell_id: str) -> bool:
        """Cancel a running prompt cell; returns True if it was mid-run."""
        with self._run_tokens_lock:
            self._cancelled.add(cell_id)
            token = self._run_tokens.get(cell_id)
        if token is not None:
            token.cancel()
            return True
        return False

    def _run_prompt(self, cell_id: str, source: str, on_trace) -> dict:
        """Run a prompt cell through the agent, streaming trace steps.

        Returns a dict with ``output``/``stopped``/``error`` plus ``usage``,
        ``run_id``, and ``conversation_id`` for the UI and trace persistence.
        """
        with self._run_tokens_lock:
            if cell_id in self._cancelled:
                self._cancelled.discard(cell_id)
                return {
                    "output": None,
                    "stopped": True,
                    "error": None,
                    "usage": None,
                    "run_id": None,
                    "conversation_id": None,
                }
            token = CancellationToken()
            self._run_tokens[cell_id] = token
        try:
            return asyncio.run(self._run_prompt_async(cell_id, source, token, on_trace))
        except RunCancelled:
            self._finish_trace(cell_id, status="stopped")
            return {
                "output": None,
                "stopped": True,
                "error": None,
                "usage": None,
                "run_id": None,
                "conversation_id": None,
            }
        except Exception as exc:  # noqa: BLE001 - surface failures in the cell output
            self._finish_trace(cell_id, status="error", error=str(exc))
            return {
                "output": None,
                "stopped": False,
                "error": str(exc),
                "usage": None,
                "run_id": None,
                "conversation_id": None,
            }
        finally:
            with self._run_tokens_lock:
                self._run_tokens.pop(cell_id, None)
                self._cancelled.discard(cell_id)

    async def _run_prompt_async(self, cell_id: str, source: str, token, on_trace) -> dict:
        agent = current_agent()
        trace_path = self._trace_path(cell_id)
        run_id = uuid.uuid4().hex
        if trace_path is not None:
            trace.write_run(
                trace_path,
                cell_id=cell_id,
                run_id=run_id,
                model=self.model,
                prompt=source,
            )

        plan_store = current_plan_store()
        if plan_store is not None:
            await plan_store.set_items([])

        async def on_events(ctx, events):  # noqa: ARG001 - ctx unused
            async for event in events:
                step = _event_to_step(event)
                if step is not None:
                    on_trace(step)
                    if trace_path is not None:
                        trace.append_step(trace_path, step)
                    self.broadcast("trace", {"id": cell_id, "step": step})
                if (
                    plan_store is not None
                    and getattr(event, "event_kind", None) == "function_tool_result"
                    and getattr(getattr(event, "part", None), "tool_name", None) in _PLAN_TOOLS
                ):
                    items = [i.model_dump(mode="json") for i in await plan_store.get_items()]
                    plan_step = {"type": "plan", "items": items}
                    on_trace(plan_step)
                    if trace_path is not None:
                        trace.append_step(trace_path, plan_step)
                    self.broadcast("trace", {"id": cell_id, "step": plan_step})

        source = self._augment_source(source)
        result = await agent.run(
            source,
            event_stream_handler=on_events,
            cancellation_token=token,
            run_id=run_id,
        )

        usage = trace.usage_to_dict(result.usage)
        usage_step = {"type": "usage", "usage": usage}
        on_trace(usage_step)
        self.broadcast("trace", {"id": cell_id, "step": usage_step})
        if trace_path is not None:
            trace.append_step(trace_path, usage_step)
            trace.append_messages(trace_path, result.new_messages())
            trace.append_result(
                trace_path,
                status="done",
                output=str(result.output),
                usage=usage,
                conversation_id=result.conversation_id,
            )
        return {
            "output": str(result.output),
            "stopped": False,
            "error": None,
            "usage": usage,
            "run_id": run_id,
            "conversation_id": result.conversation_id,
        }


    def _run_worker(self) -> None:
        while True:
            cell_id = self._run_q.get()
            try:
                with self.lock:
                    try:
                        cell = self._find_cell(cell_id)
                    except KeyError:
                        continue
                    self._execute_cell(cell)
                    self.broadcast("cell", cell)
                    self.broadcast("map", {"project": self.map.to_project()})
                    self.broadcast("files", {"files": self.list_files()})
            except Exception:  # noqa: BLE001 - never let a run kill the worker
                traceback.print_exc()
            finally:
                self._run_q.task_done()

    # -- import / files ----------------------------------------------------

    def import_local(self, uploaded: list[tuple[str, bytes]]) -> dict:
        with self.lock:
            if self.workspace is None:
                raise ValueError("no workspace open")
            imported: list[str] = []
            for filename, data in uploaded:
                if not filename:
                    raise ValueError("empty filename")
                dest = self.workspace.resolve_under(self.workspace.data, filename)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                imported.append(dest.relative_to(self.workspace.root).as_posix())
            self.broadcast("files", {"files": self.list_files()})
            return {"imported": imported}

    def import_url(self, url: str, filename: str | None = None) -> dict:
        with self.lock:
            if self.workspace is None:
                raise ValueError("no workspace open")
            name = filename or _filename_from_url(url)
            abs_path = download(url, name)
            rel = Path(abs_path).relative_to(self.workspace.root).as_posix()
            self.broadcast("files", {"files": self.list_files()})
            return {"path": rel}

    def list_files(self) -> list[str]:
        with self.lock:
            return self.workspace.list_files() if self.workspace else []

    def _augment_source(self, source: str) -> str:
        """Prepend the ``data/`` listing to the current user turn."""
        if self.workspace is None:
            return source
        files = self.workspace.list_files("data")
        if not files:
            return source
        listing = "\n".join(f"- {f}" for f in files)
        context = (
            "Files currently available in the workspace data/ folder "
            "(imported inputs the user may refer to):\n" + listing
        )
        return context + "\n\n" + source

    def set_map_project(self, project: dict) -> None:
        with self.lock:
            if self.workspace is None:
                raise ValueError("no workspace open")
            self.map.load_project(project)
            self.map.save_project(str(self.workspace.maps / _SNAPSHOT))
            self.workspace.bump()

    # -- snapshot / broadcast ----------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "active_workspace": self.active_name,
                "workspaces": list_workspaces(),
                "cells": self.cells,
                "map_project": self.map.to_project(),
                "map_app_url": self.map._app_url,
                "files": self.workspace.list_files() if self.workspace else [],
                "settings": dict(self.settings),
            }

    def broadcast(self, event: str, data: dict) -> None:
        with self._subscribers_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put({"event": event, "data": data})
            except Exception:  # noqa: BLE001 - drop a dead queue
                with self._subscribers_lock:
                    try:
                        self._subscribers.remove(q)
                    except ValueError:
                        pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._subscribers_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subscribers_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

load_env()

state = AppState()
