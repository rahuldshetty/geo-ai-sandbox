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
    ModelAPIError,
    ModelHTTPError,
    RunCancelled,
)
from pydantic_ai_harness.planning import PlanItem


from .. import trace
from ..agent import build_agent, current_agent, current_plan_store
from ..config import list_workspaces, load_env, workspace_root
from ..context import GeoContext, current, set_context
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

# Only these tools are known to leave the workspace and live map unchanged.
# Unknown and newly added tools are treated conservatively as state-changing
# until they are explicitly reviewed and added here.
_REPLAY_SAFE_TOOLS = frozenset(
    {
        "describe_geolibre_bridge",
        "describe_map",
        "discover_capabilities",
        "find_files",
        "inspect_output",
        "list_colormaps",
        "list_files",
        "python_help",
        "query_output",
        "raster_info",
        "raster_stats",
        "read_file",
        "read_plan",
        "read_vector",
        "request_user_input",
        "sample_point",
        "search_openaerialmap",
        "search_tools",
        "search_vantor_events",
        "search_vantor_imagery",
    }
).union(_PLAN_TOOLS)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429})


def _tool_may_change_state(name: str | None) -> bool:
    return not name or name not in _REPLAY_SAFE_TOOLS


def _is_transient_run_error(error: Exception) -> bool:
    """Return whether replaying a workspace/map-safe attempt may succeed."""
    if isinstance(error, ModelHTTPError):
        return (
            error.status_code in _TRANSIENT_HTTP_STATUSES
            or error.status_code >= 500
        )
    # Non-HTTP ModelAPIError instances represent provider/transport failures.
    return isinstance(error, ModelAPIError)


def _retry_delay(error: Exception, attempt: int) -> float:
    """Return bounded provider-aware backoff seconds for a retry."""
    if isinstance(error, ModelHTTPError) and error.retry_after is not None:
        return min(error.retry_after, 30.0)
    return min(0.5 * (2 ** (attempt - 1)), 8.0)


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


def _is_generated_cell(cell: dict) -> bool:
    geoai = (cell.get("metadata") or {}).get("geoai") or {}
    return bool(geoai.get("generated"))


def _ordered_notebook_cells(cells: list[dict], provenance: list[dict]) -> list[dict]:
    """Place hidden provenance directly after its parent in the saved notebook."""
    by_parent: dict[str, list[dict]] = {}
    orphaned: list[dict] = []
    visible_ids = {cell["id"] for cell in cells}
    for recorded in provenance:
        geoai = (recorded.get("metadata") or {}).get("geoai") or {}
        parent_id = geoai.get("parent_cell_id")
        if parent_id in visible_ids:
            by_parent.setdefault(parent_id, []).append(recorded)
        else:
            orphaned.append(recorded)
    ordered = []
    for cell in cells:
        ordered.append(cell)
        ordered.extend(by_parent.get(cell["id"], []))
    ordered.extend(orphaned)
    return ordered


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
            "outcome": getattr(part, "outcome", None),
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
        self.provenance_cells: list[dict] = []
        self._subscribers: list[queue.Queue] = []
        self._subscribers_lock = threading.Lock()
        self._run_q: queue.Queue = queue.Queue()
        self._run_tokens: dict[str, CancellationToken] = {}
        self._cancelled: set[str] = set()
        self._queued: set[str] = set()
        self._resume_payloads: dict[str, dict] = {}
        self._run_tokens_lock = threading.Lock()
        self._download_cells: dict[str, dict] = {}
        self._download_lock = threading.RLock()

        worker = threading.Thread(target=self._run_worker, name="geoai-run-worker", daemon=True)
        worker.start()

    # -- workspace lifecycle ---------------------------------------------

    def open_workspace(self, name: str) -> None:
        with self.lock:
            ws = Workspace(workspace_root(name)).create()
            self.workspace = ws
            notebook_cells = read_nb(ws.root / "notebook.ipynb")
            self.cells = [
                cell
                for cell in notebook_cells
                if not _is_generated_cell(cell) and cell.get("kind") != "interaction"
            ]
            self.provenance_cells = [
                cell for cell in notebook_cells if _is_generated_cell(cell)
            ]
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
            with self._download_lock:
                self._download_cells.clear()
            ctx = self._new_context(ws)
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
            self.provenance_cells = []
            with self._download_lock:
                self._download_cells.clear()
            set_context(None)
            self.map.load_project(
                _project.build_empty_project(center=(0, 0), zoom=2)
            )

    def save_workspace(self) -> dict:
        with self.lock:
            if self.workspace is not None:
                self._save_cells()
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
                    ctx = self._new_context(self.workspace)
                    set_context(ctx)
                    build_agent(ctx, self.model)
            except Exception as exc:  # noqa: BLE001 - roll back and surface
                self.settings["model"] = old_model
                self.model = old_model
                self.settings = save_settings(self.settings)
                raise ValueError(f"could not apply settings: {exc}") from exc
            set_dangerous_mode(self.settings.get("dangerous_mode", False))
            return dict(self.settings)

    def _new_context(self, workspace: Workspace) -> GeoContext:
        """Build the shared tool context, including download progress routing."""
        return GeoContext(
            map=self.map,
            workspace=workspace,
            version=None,
            download_progress=self._on_download_progress,
        )

    # -- download progress --------------------------------------------------

    def _on_download_progress(self, event: dict) -> None:
        """Update a reusable download cell without blocking the run worker.

        Prompt execution holds ``state.lock`` for its full duration. Progress
        callbacks also arrive from parallel download threads, so this path uses
        its own lock and only broadcasts immutable snapshots; it never waits for
        the main state lock.
        """
        job_id = event.get("id")
        if not job_id:
            return
        files = None
        with self._download_lock:
            cell = self._download_cells.get(job_id)
            if cell is None:
                cell = {
                    "id": job_id,
                    "kind": "download",
                    "parent_cell_id": event.get("parent_cell_id"),
                    "filename": event.get("filename") or "download",
                    "status": "running",
                    "bytes_downloaded": 0,
                    "total_bytes": None,
                    "path": None,
                    "error": None,
                }
                self._download_cells[job_id] = cell
            for key in (
                "parent_cell_id",
                "filename",
                "status",
                "bytes_downloaded",
                "total_bytes",
                "path",
                "error",
            ):
                if key in event:
                    cell[key] = event[key]
            snapshot = dict(cell)
            if snapshot.get("status") == "done" and self.workspace is not None:
                # Do not call self.list_files() here: prompt execution holds
                # state.lock while a parallel download worker emits this event.
                # Reading the workspace directly keeps the completion event
                # from waiting on the run worker that is waiting on the download.
                files = self.workspace.list_files()
        self.broadcast("download", snapshot)
        if files is not None:
            self.broadcast("files", {"files": files})

    def _downloads_snapshot(self) -> list[dict]:
        with self._download_lock:
            return [dict(cell) for cell in self._download_cells.values()]

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
            if loaded["status"] == "done" and loaded["output"] is not None:
                cell["status"] = "done"
                cell["outputs"] = [_stream_output(str(loaded["output"]))]
            elif loaded["status"] == "error" and loaded["error"] is not None:
                cell["status"] = "error"
                cell["outputs"] = [
                    _error_output("AgentError", f"ERROR: {loaded['error']}")
                ]
            elif loaded["status"] == "stopped":
                cell["status"] = "stopped"
                cell["outputs"] = [_stream_output("Stopped.")]

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
            write_nb(
                self.workspace.root / "notebook.ipynb",
                _ordered_notebook_cells(self.cells, self.provenance_cells),
            )

    def _append_recorded_cell(self, cell: dict) -> None:
        """Persist agent provenance without adding it to the interactive cell list."""
        self.provenance_cells.append(cell)
        self._save_cells()

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
        for cell in reversed(self.provenance_cells):
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
        """Complete the deferred tool record and save the user's choices."""
        call_id = interaction.get("tool_call_id")
        for recorded in reversed(self.provenance_cells):
            geoai = (recorded.get("metadata") or {}).get("geoai") or {}
            if geoai.get("tool_call_id") == call_id:
                self._record_tool_result(
                    recorded,
                    {"content": {"user_response": answers}},
                )
                break
        fields = {
            field.get("id"): field
            for field in interaction.get("fields", [])
        }
        lines = ["### Input provided"]
        for key, value in answers.items():
            field = fields.get(key) or {}
            option_labels = {
                option.get("value"): option.get("label", option.get("value"))
                for option in field.get("options", [])
            }
            values = value if isinstance(value, list) else [value]
            rendered = ", ".join(str(option_labels.get(item, item)) for item in values)
            lines.append(f"- **{field.get('label', key)}:** {rendered}")
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
            self.provenance_cells = [
                recorded
                for recorded in self.provenance_cells
                if ((recorded.get("metadata") or {}).get("geoai") or {}).get(
                    "parent_cell_id"
                )
                != cell_id
            ]
            with self._download_lock:
                self._download_cells = {
                    job_id: download
                    for job_id, download in self._download_cells.items()
                    if download.get("parent_cell_id") != cell_id
                }
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
            # A fresh run supersedes any interaction left pending by a prior run.
            cell["interaction"] = None
            cell["interaction_history"] = []
            cell.setdefault("metadata", {}).setdefault("geoai", {}).pop("interaction", None)
            self.provenance_cells = [
                recorded
                for recorded in self.provenance_cells
                if ((recorded.get("metadata") or {}).get("geoai") or {}).get(
                    "parent_cell_id"
                )
                != cell_id
            ]
            with self._download_lock:
                self._download_cells = {
                    job_id: download
                    for job_id, download in self._download_cells.items()
                    if download.get("parent_cell_id") != cell_id
                }
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
        ctx = None
        try:
            ctx = current()
            ctx.download_parent_id = cell["id"] if kind == "prompt" else None
        except RuntimeError:
            pass

        try:
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
            else:
                trace_steps: list[dict] = list(cell.get("trace", [])) if resume else []
                result = self._run_prompt(cell["id"], source, trace_steps, resume=resume)
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
                    cell["outputs"] = [_stream_output(result.get("output") or "")]
        finally:
            if ctx is not None:
                ctx.download_parent_id = None

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
        self, cell_id: str, source: str, trace_steps: list[dict], *, resume: dict | None = None
    ) -> dict:
        """Run a prompt cell through the agent, streaming trace steps.

        ``trace_steps`` is the cell's live step list: new steps are appended to
        it as they stream, and on resume its existing entries are the prior
        segment's steps (used to restore the plan snapshot).

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
                self._run_prompt_async(cell_id, source, token, trace_steps, resume=resume)
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
        self, cell_id: str, source: str, token, trace_steps: list[dict], *, resume: dict | None = None
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
        initial_plan_items = _latest_plan_items(trace_steps) if resume else []
        if plan_store is not None:
            # The store is shared by the single worker. A different prompt may
            # run while this one waits for user input, and a server restart
            # recreates the in-memory store. Restore this prompt's own latest
            # snapshot before resuming instead of inheriting another run's plan.
            await plan_store.set_items(initial_plan_items)
        recorded_tools: dict[str, dict] = {}
        attempt_changed_state = False

        async def on_events(ctx, events):  # noqa: ARG001 - ctx unused
            nonlocal attempt_changed_state
            async for event in events:
                step = _event_to_step(event)
                if step is not None:
                    tool_call_id = step.get("tool_call_id")
                    # A resumed run re-emits the deferred tool call it is
                    # answering; that step is already in the trace, so only
                    # its (new) result should be appended.
                    replayed = (
                        step.get("type") == "tool_call"
                        and tool_call_id is not None
                        and any(
                            s.get("type") == "tool_call"
                            and s.get("tool_call_id") == tool_call_id
                            for s in trace_steps
                        )
                    )
                    if not replayed:
                        trace_steps.append(step)
                        if trace_path is not None:
                            trace.append_step(trace_path, step)
                        self.broadcast("trace", {"id": cell_id, "step": step})
                    if self.settings.get("record_agent_steps", True):
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
                        step.get("type") == "tool_result"
                        and step.get("outcome") == "success"
                        and _tool_may_change_state(step.get("name"))
                    ):
                        attempt_changed_state = True
                if (
                    plan_store is not None
                    and getattr(event, "event_kind", None) == "function_tool_result"
                    and getattr(getattr(event, "part", None), "tool_name", None) in _PLAN_TOOLS
                ):
                    items = [i.model_dump(mode="json") for i in await plan_store.get_items()]
                    plan_step = {"type": "plan", "items": items}
                    trace_steps.append(plan_step)
                    if trace_path is not None:
                        trace.append_step(trace_path, plan_step)
                    self.broadcast("trace", {"id": cell_id, "step": plan_step})

        source = self._augment_source(source) if resume is None else None
        max_attempts = self.settings.get("max_retries", 5)
        last_error = None
        retry_block_reason = None

        for attempt in range(1, max_attempts + 1):
            attempt_changed_state = False
            if attempt > 1 and plan_store is not None:
                await plan_store.set_items(initial_plan_items)
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
                transient = _is_transient_run_error(exc)
                retry_block_reason = (
                    "automatic replay was skipped because a tool changed the "
                    "workspace or map; those completed changes were preserved"
                    if attempt_changed_state
                    else None
                )
                if transient and not attempt_changed_state and attempt < max_attempts:
                    note = {
                        "type": "text",
                        "content": (
                            f"Attempt {attempt}/{max_attempts} failed "
                            f"({type(exc).__name__}: {exc}). Retrying."
                        ),
                    }
                    trace_steps.append(note)
                    if trace_path is not None:
                        trace.append_step(trace_path, note)
                    self.broadcast("trace", {"id": cell_id, "step": note})
                    await asyncio.sleep(_retry_delay(exc, attempt))
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
                trace_steps.append(usage_step)
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
        if retry_block_reason is not None:
            error_text = f"{error_text} ({retry_block_reason})"
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
            self._record_interaction_response(cell_id, interaction, answers)
            cell["status"] = "running"
            cell["interaction"] = None
            completed = dict(interaction)
            completed["answers"] = answers
            completed["submitted"] = True
            cell.setdefault("interaction_history", []).append(completed)
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
            rel = download(url, name)
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
                "downloads": self._downloads_snapshot(),
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
