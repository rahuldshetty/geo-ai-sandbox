"""FastAPI app exposing the Geo-AI web UI and its JSON/SSE API."""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import list_workspaces
from .state import state

STATIC_DIR = Path(__file__).parent / "static"

_VALID_KINDS = frozenset({"markdown", "python", "prompt"})

app = FastAPI()


# -- request bodies ---------------------------------------------------------


class NewWorkspace(BaseModel):
    name: str


class OpenWorkspace(BaseModel):
    name: str


class AddCell(BaseModel):
    kind: str
    source: str = ""
    index: int | None = None


class UpdateCell(BaseModel):
    source: str


class MoveCell(BaseModel):
    index: int


class ImportUrl(BaseModel):
    url: str
    filename: str | None = None


class MapProject(BaseModel):
    project: dict


# -- static -----------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# -- state ------------------------------------------------------------------


@app.get("/api/state")
def api_state() -> dict:
    return state.snapshot()


# -- workspace ---------------------------------------------------------------


@app.post("/api/workspace/new")
def api_workspace_new(body: NewWorkspace) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="workspace name is required")
    with state.lock:
        state.new_workspace(name)
        return state.snapshot()


@app.post("/api/workspace/open")
def api_workspace_open(body: OpenWorkspace) -> dict:
    if body.name not in list_workspaces():
        raise HTTPException(status_code=404, detail="workspace not found")
    with state.lock:
        state.open_workspace(body.name)
        return state.snapshot()


@app.post("/api/workspace/save")
def api_workspace_save() -> dict:
    if state.workspace is None:
        raise HTTPException(status_code=409, detail="no workspace open")
    with state.lock:
        return state.save_workspace()


@app.post("/api/workspace/close")
def api_workspace_close() -> dict:
    with state.lock:
        state.close_workspace()
        return state.snapshot()


# -- cells -------------------------------------------------------------------


@app.post("/api/cells")
def api_add_cell(body: AddCell) -> dict:
    if body.kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail="invalid cell kind")
    with state.lock:
        state.add_cell(body.kind, body.source, body.index)
        return state.snapshot()


@app.put("/api/cells/{cell_id}")
def api_update_cell(cell_id: str, body: UpdateCell) -> dict:
    with state.lock:
        try:
            state.update_cell(cell_id, body.source)
        except KeyError:
            raise HTTPException(status_code=404, detail="cell not found")
        return state.snapshot()


@app.delete("/api/cells/{cell_id}")
def api_delete_cell(cell_id: str) -> dict:
    with state.lock:
        try:
            state.delete_cell(cell_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="cell not found")
        return state.snapshot()


@app.post("/api/cells/{cell_id}/move")
def api_move_cell(cell_id: str, body: MoveCell) -> dict:
    with state.lock:
        try:
            state.move_cell(cell_id, body.index)
        except KeyError:
            raise HTTPException(status_code=404, detail="cell not found")
        return state.snapshot()


@app.post("/api/cells/{cell_id}/run")
def api_run_cell(cell_id: str) -> dict:
    with state.lock:
        try:
            state.run_cell(cell_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="cell not found")
        except ValueError:
            raise HTTPException(status_code=400, detail="markdown cells are not runnable")
        return {"accepted": True}


@app.post("/api/cells/{cell_id}/stop")
def api_stop_cell(cell_id: str) -> dict:
    # No state.lock here: a running cell holds it for its full duration, so a
    # stop request must not block behind it. stop_cell only touches the run
    # token registry (its own lock) and cancels the agent via a thread-safe
    # CancellationToken.
    return {"stopped": state.stop_cell(cell_id)}

@app.post("/api/run-all")
def api_run_all() -> dict:
    if state.workspace is None:
        raise HTTPException(status_code=409, detail="no workspace open")
    with state.lock:
        state.run_all()
        return {"accepted": True}


# -- import / map ------------------------------------------------------------


@app.post("/api/import/local")
async def api_import_local(files: list[UploadFile] = File(...)) -> dict:
    if state.workspace is None:
        raise HTTPException(status_code=409, detail="no workspace open")
    uploaded = []
    for f in files:
        data = await f.read()
        uploaded.append((f.filename or "", data))
    with state.lock:
        try:
            return state.import_local(uploaded)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/import/url")
def api_import_url(body: ImportUrl) -> dict:
    if state.workspace is None:
        raise HTTPException(status_code=409, detail="no workspace open")
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    with state.lock:
        return state.import_url(body.url, body.filename)


@app.post("/api/map/project")
def api_set_map_project(body: MapProject) -> dict:
    if state.workspace is None:
        raise HTTPException(status_code=409, detail="no workspace open")
    with state.lock:
        state.set_map_project(body.project)
        return {"ok": True}


# -- SSE ---------------------------------------------------------------------


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    q = state.subscribe()
    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.to_thread(q.get, timeout=0.5)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
        finally:
            state.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
