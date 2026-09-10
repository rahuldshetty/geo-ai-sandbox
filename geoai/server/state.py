"""Process-wide server state: the live map, the active workspace, and cells.

A single ``AppState`` singleton owns the GeoLibre map and notebook cells, guarded
by one ``threading.RLock``. FastAPI sync endpoints run in a threadpool and wrap
every mutation in ``state.lock``; cell runs hold the lock for their full
duration, so workspace open/close/save blocks until the current run finishes
(acceptable for a single-user desktop app).
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import traceback
import uuid
from pathlib import Path
from pprint import pformat
from urllib.parse import urlparse

from geolibre import Map
from geolibre import project as _project
from pydantic_ai import (
    CancellationToken,
    DeferredToolRequests,
    DeferredToolResults,
    RunCancelled,
)
from pydantic_ai_harness.planning import PlanItem


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


def _latest_plan_items(steps: list[dict]) -> list[PlanItem]:
    """Rebuild the latest plan snapshot from a prompt's persisted trace."""
    for step in reversed(steps):
        if not isinstance(step, dict) or step.get("type") != "plan":
            continue
        items = step.get("items")
        if not isinstance(items, list):
            return []
        restored = []
        for item in items:
            try:
                restored.append(PlanItem.model_validate(item))
            except (TypeError, ValueError):
                continue
        return restored
    return []


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
        self._queued: set[str] = set()
        self._resume_payloads: dict[str, dict] = {}
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
            # Local raster layers persisted under the old geolibre session-token
            # scheme carry non-durable URLs; re-point them to this server's stable
            # /api/files route (needs the context bound first, since repoint uses
            # `current()` to build the URL).
            ctx = GeoContext(map=self.map, workspace=ws, version=None)
            set_context(ctx)
            repoint_local_rasters(self.map, ws)
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
            if "max_retries" in patch and patch["max_retries"] is not None:
                try:
                    self.settings["max_retries"] = max(1, int(patch["max_retries"]))
                except (TypeError, ValueError):
                    self.settings["max_retries"] = 5
            if "record_agent_steps" in patch and patch["record_agent_steps"] is not None:
                self.settings["record_agent_steps"] = bool(patch["record_agent_steps"])
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

    def _append_recorded_cell(self, cell: dict) -> None:
        """Append and persist an agent-generated provenance cell."""
        self.cells.append(cell)
        self._save_cells()
        self.broadcast("cell", cell)

    def _record_tool_call(self, parent_id: str, step: dict) -> dict:
        """Create a read-only code cell for a structured agent tool call."""
        name = step.get("name") or "unknown_tool"
        args = step.get("args")
        if name == "run_python" and isinstance(args, dict) and isinstance(args.get("code"), str):
            source = args["code"]
        else:
            rendered = pformat(args if isinstance(args, dict) else {}, sort_dicts=False)
            source = (
                f"# GeoAI structured tool call: {name}\n"
                f"# Replayed by the Geo-AI harness with the active workspace context.\n"
                f"{name}(**{rendered})"
            )
        cell = new_cell(
            "tool",
            source,
            metadata={
                "geoai": {
                    "kind": "tool",
                    "role": "assistant",
                    "parent_cell_id": parent_id,
                    "tool_name": name,
                    "tool_call_id": step.get("tool_call_id"),
                    "args": args,
                    "generated": True,
                }
            },
        )
        cell["status"] = "running"
        self._append_recorded_cell(cell)
        return cell

    def _recorded_tool_cell(self, tool_call_id: str) -> dict | None:
        """Find a provenance cell for a tool call already recorded before a resume."""
        for cell in reversed(self.cells):
            geoai = (cell.get("metadata") or {}).get("geoai") or {}
            if geoai.get("tool_call_id") == tool_call_id:
                return cell
        return None

    def _record_tool_result(self, cell: dict, step: dict) -> None:
        """Attach a tool result to its previously recorded code cell."""
        content = step.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, indent=2, ensure_ascii=False)
        cell["outputs"] = [_stream_output(text)]
        cell["execution_count"] = 1
        cell["status"] = "done"
        self._save_cells()
        self.broadcast("cell", cell)

    def _record_agent_response(self, parent_id: str, output: str) -> None:
        """Append the agent's final response as a standard Markdown cell."""
        cell = new_cell(
            "markdown",
            output,
            metadata={
                "geoai": {
                    "kind": "response",
                    "role": "assistant",
                    "parent_cell_id": parent_id,
                    "generated": True,
                }
            },
        )
        cell["status"] = "done"
        self._append_recorded_cell(cell)

    def _record_interaction_response(
        self, parent_id: str, interaction: dict, answers: dict
    ) -> None:
        """Complete the deferred tool cell and append the user's choices."""
        call_id = interaction.get("tool_call_id")
        for recorded in reversed(self.cells):
            geoai = (recorded.get("metadata") or {}).get("geoai") or {}
            if geoai.get("tool_call_id") == call_id:
                self._record_tool_result(
                    recorded,
                    {"content": {"user_response": answers}},
                )
                break
        labels = {
            field.get("id"): field.get("label", field.get("id"))
            for field in interaction.get("fields", [])
        }
        lines = ["### Input provided"]
        for key, value in answers.items():
            rendered = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            lines.append(f"- **{labels.get(key, key)}:** {rendered}")
        cell = new_cell(
            "markdown",
            "\n".join(lines),
            metadata={
                "geoai": {
                    "kind": "interaction_response",
                    "role": "user",
                    "parent_cell_id": parent_id,
                    "interaction_id": interaction.get("id"),
                    "generated": True,
                }
            },
        )
        cell["status"] = "done"
        self._append_recorded_cell(cell)

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
            with self._run_tokens_lock:
                self._queued.add(cell_id)
            self._run_q.put(cell_id)

    def run_all(self) -> None:
        with self.lock:
            for cell in self.cells:
                if cell["kind"] in _RUNNABLE_KINDS:
                    with self._run_tokens_lock:
                        self._queued.add(cell["id"])
                    self._run_q.put(cell["id"])

    def _execute_cell(self, cell: dict, resume: dict | None = None) -> None:
        source = cell["source"]
        kind = cell["kind"]
        prev = cell.get("execution_count")
        cell["execution_count"] = (prev or 0) + 1

        if kind == "python":
            with self._run_tokens_lock:
                self._queued.discard(cell["id"])
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
            trace_steps: list[dict] = list(cell.get("trace", [])) if resume else []
            result = self._run_prompt(cell["id"], source, trace_steps.append, resume=resume)
            cell["trace"] = trace_steps
            cell["usage"] = result.get("usage")
            cell["run_id"] = result.get("run_id")
            cell["conversation_id"] = result.get("conversation_id")
            if result.get("waiting"):
                cell["status"] = "waiting_for_input"
                cell["interaction"] = result["interaction"]
                cell.setdefault("metadata", {}).setdefault("geoai", {})["interaction"] = result[
                    "interaction"
                ]
                cell["outputs"] = []
            elif result.get("stopped"):
                cell["status"] = "stopped"
                cell["outputs"] = [_stream_output("Stopped.")]
            elif result.get("error") is not None:
                cell["status"] = "error"
                cell["outputs"] = [_error_output("AgentError", f"ERROR: {result['error']}")]
            else:
                cell["status"] = "done"
                if not self.settings.get("record_agent_steps", True):
                    cell["outputs"] = [_stream_output(result.get("output") or "")]

    def stop_cell(self, cell_id: str) -> bool:
        """Cancel a running or queued prompt cell; returns True if it was stopped."""
        with self._run_tokens_lock:
            token = self._run_tokens.get(cell_id)
            if token is None:
                if cell_id in self._queued:
                    self._cancelled.add(cell_id)
                    return True
                return False
        token.cancel()
        return True


    def _run_prompt(
        self, cell_id: str, source: str, on_trace, *, resume: dict | None = None
    ) -> dict:
        """Run a prompt cell through the agent, streaming trace steps.

        Returns a dict with ``output``/``stopped``/``error`` plus ``usage``,
        ``run_id``, and ``conversation_id`` for the UI and trace persistence.
        """
        with self._run_tokens_lock:
            self._queued.discard(cell_id)
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
            return asyncio.run(
                self._run_prompt_async(cell_id, source, token, on_trace, resume=resume)
            )
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

    async def _run_prompt_async(
        self, cell_id: str, source: str, token, on_trace, *, resume: dict | None = None
    ) -> dict:
        agent = current_agent()
        trace_path = self._trace_path(cell_id)
        run_id = uuid.uuid4().hex
        if trace_path is not None and resume is None:
            trace.write_run(
                trace_path,
                cell_id=cell_id,
                run_id=run_id,
                model=self.model,
                prompt=source,
            )
        elif trace_path is not None:
            trace.append_resume(
                trace_path,
                run_id=run_id,
                conversation_id=resume.get("conversation_id"),
                response=resume.get("answers") or {},
            )

        plan_store = current_plan_store()
        if plan_store is not None:
            # The store is shared by the single worker. A different prompt may
            # run while this one waits for user input, and a server restart
            # recreates the in-memory store. Restore this prompt's own latest
            # snapshot before resuming instead of inheriting another run's plan.
            plan_items = _latest_plan_items(trace_steps) if resume else []
            await plan_store.set_items(plan_items)
        recorded_tools: dict[str, dict] = {}

        async def on_events(ctx, events):  # noqa: ARG001 - ctx unused
            async for event in events:
                step = _event_to_step(event)
                if step is not None:
                    on_trace(step)
                    if trace_path is not None:
                        trace.append_step(trace_path, step)
                    self.broadcast("trace", {"id": cell_id, "step": step})
                    if self.settings.get("record_agent_steps", True):
                        tool_call_id = step.get("tool_call_id")
                        if step.get("type") == "tool_call" and tool_call_id:
                            existing = self._recorded_tool_cell(tool_call_id)
                            recorded_tools[tool_call_id] = existing or self._record_tool_call(
                                cell_id, step
                            )
                        elif step.get("type") == "tool_result" and tool_call_id:
                            recorded = recorded_tools.get(tool_call_id)
                            if recorded is not None:
                                self._record_tool_result(recorded, step)
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

        source = self._augment_source(source) if resume is None else None
        max_attempts = self.settings.get("max_retries", 5)
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = await agent.run(
                    source,
                    message_history=resume.get("messages") if resume else None,
                    deferred_tool_results=resume.get("deferred_results") if resume else None,
                    conversation_id=resume.get("conversation_id") if resume else None,
                    event_stream_handler=on_events,
                    cancellation_token=token,
                    run_id=run_id,
                )
            except RunCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - retry transient run failures
                last_error = exc
                if attempt < max_attempts:
                    note = {
                        "type": "text",
                        "content": (
                            f"Attempt {attempt}/{max_attempts} failed "
                            f"({type(exc).__name__}: {exc}). Retrying."
                        ),
                    }
                    on_trace(note)
                    if trace_path is not None:
                        trace.append_step(trace_path, note)
                    self.broadcast("trace", {"id": cell_id, "step": note})
                    continue
                break
            else:
                usage = trace.usage_to_dict(result.usage)
                if isinstance(result.output, DeferredToolRequests):
                    interaction = self._interaction_from_deferred(result.output)
                    recorded = self._recorded_tool_cell(interaction["tool_call_id"])
                    if recorded is not None:
                        recorded["status"] = "waiting_for_input"
                        self._save_cells()
                        self.broadcast("cell", recorded)
                    if trace_path is not None:
                        trace.append_messages(trace_path, result.all_messages())
                        trace.append_result(
                            trace_path,
                            status="waiting_for_input",
                            usage=usage,
                            conversation_id=result.conversation_id,
                        )
                    return {
                        "output": None,
                        "waiting": True,
                        "interaction": interaction,
                        "stopped": False,
                        "error": None,
                        "usage": usage,
                        "run_id": run_id,
                        "conversation_id": result.conversation_id,
                    }
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
                if self.settings.get("record_agent_steps", True):
                    self._record_agent_response(cell_id, str(result.output))
                return {
                    "output": str(result.output),
                    "stopped": False,
                    "error": None,
                    "usage": usage,
                    "run_id": run_id,
                    "conversation_id": result.conversation_id,
                }

        error_text = str(last_error) if last_error is not None else "unknown error"
        self._finish_trace(cell_id, status="error", error=error_text)
        return {
            "output": None,
            "stopped": False,
            "error": error_text,
            "usage": None,
            "run_id": None,
            "conversation_id": None,
        }

    @staticmethod
    def _interaction_from_deferred(requests: DeferredToolRequests) -> dict:
        """Normalize one deferred request into the browser interaction schema."""
        if not requests.calls:
            raise RuntimeError("agent requested approval without an external interaction")
        call = requests.calls[0]
        metadata = requests.metadata.get(call.tool_call_id, {})
        form = metadata.get("interaction")
        if not isinstance(form, dict):
            args = call.args if isinstance(call.args, dict) else {}
            form = {
                "title": args.get("title", "Input required"),
                "prompt": args.get("prompt", ""),
                "fields": args.get("fields", []),
                "submit_label": args.get("submit_label", "Continue"),
                "allow_cancel": args.get("allow_cancel", True),
            }
        return {
            "id": uuid.uuid4().hex,
            "tool_call_id": call.tool_call_id,
            **form,
        }

    def respond_interaction(self, cell_id: str, interaction_id: str, answers: dict) -> None:
        """Queue a paused prompt to resume with structured browser answers."""
        with self.lock:
            cell = self._find_cell(cell_id)
            interaction = cell.get("interaction")
            if (
                cell.get("status") != "waiting_for_input"
                or not interaction
                or interaction.get("id") != interaction_id
            ):
                raise ValueError("interaction is no longer pending")
            field_ids = {field.get("id") for field in interaction.get("fields", [])}
            unknown = set(answers) - field_ids
            if unknown:
                raise ValueError(f"unknown interaction fields: {sorted(unknown)}")
            missing = {
                field.get("id")
                for field in interaction.get("fields", [])
                if field.get("required", True)
                and field.get("id") not in answers
                and field.get("default") is None
            }
            if missing:
                raise ValueError(f"missing required fields: {sorted(missing)}")
            for field in interaction.get("fields", []):
                if field.get("type") not in {"radio", "multi_select"}:
                    continue
                allowed = {option.get("value") for option in field.get("options", [])}
                answer = answers.get(field.get("id"), field.get("default"))
                selected = answer if isinstance(answer, list) else [answer]
                invalid = {value for value in selected if value is not None and value not in allowed}
                if invalid:
                    raise ValueError(
                        f"invalid value for {field.get('label', field.get('id'))}: "
                        f"{sorted(invalid)}"
                    )
            trace_path = self._trace_path(cell_id)
            messages = trace.read_messages(trace_path) if trace_path is not None else []
            if not messages:
                raise ValueError("cannot resume because conversation history is unavailable")
            call_id = interaction["tool_call_id"]
            payload = {
                "messages": messages,
                "deferred_results": DeferredToolResults(calls={call_id: answers}),
                "conversation_id": cell.get("conversation_id"),
                "answers": answers,
            }
            if self.settings.get("record_agent_steps", True):
                self._record_interaction_response(cell_id, interaction, answers)
            cell["status"] = "running"
            cell["interaction"] = None
            cell.setdefault("metadata", {}).setdefault("geoai", {}).pop("interaction", None)
            with self._run_tokens_lock:
                self._resume_payloads[cell_id] = payload
                self._queued.add(cell_id)
            self._save_cells()
            self.broadcast("cell", cell)
            self._run_q.put(cell_id)

    def cancel_interaction(self, cell_id: str, interaction_id: str) -> None:
        """Cancel a prompt that is paused for browser input."""
        with self.lock:
            cell = self._find_cell(cell_id)
            interaction = cell.get("interaction")
            if (
                cell.get("status") != "waiting_for_input"
                or not interaction
                or interaction.get("id") != interaction_id
            ):
                raise ValueError("interaction is no longer pending")
            cell["status"] = "stopped"
            cell["interaction"] = None
            cell["outputs"] = [_stream_output("Stopped while waiting for input.")]
            cell.setdefault("metadata", {}).setdefault("geoai", {}).pop("interaction", None)
            self._save_cells()
            self.broadcast("cell", cell)


    def _run_worker(self) -> None:
        while True:
            cell_id = self._run_q.get()
            try:
                with self.lock:
                    try:
                        cell = self._find_cell(cell_id)
                    except KeyError:
                        continue
                    with self._run_tokens_lock:
                        resume = self._resume_payloads.pop(cell_id, None)
                    self._execute_cell(cell, resume=resume)
                    # Execution status, count, outputs, and usage are runtime
                    # mutations too; persist them after every completed run.
                    self._save_cells()
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
