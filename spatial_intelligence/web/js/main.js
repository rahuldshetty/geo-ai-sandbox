/* Boot: mount the shell, wire SSE, load the initial snapshot. */

"use strict";

import { getState } from "./api.js";
import { closeAllMenus } from "./components/menubar.js";
import { revealInteraction } from "./components/interaction.js";
import { updateJobNode } from "./components/progress.js";
import { refreshStatusBar } from "./components/status-bar.js";
import { applyTrace } from "./components/trace.js";
import { connectEvents } from "./events.js";
import { renderCellsOnly } from "./pages/cells.js";
import { renderDataOnly } from "./pages/data.js";
import { mountShell, renderShell } from "./pages/shell.js";
import { syncMap } from "./pages/map.js";
import { applySnapshot, isGeneratedCell, normalizeCells, setFiles, setState, state, subscribe, upsertJob } from "./store.js";

export function boot() {
  mountShell(document.getElementById("app"));
  subscribe(() => renderShell());
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".menu")) closeAllMenus();
  });
  connectEvents({
    onCell: handleCell,
    onTrace: handleTrace,
    onJob: handleJob,
    onMap: handleMap,
    onFiles: handleFiles,
    onSettings: handleSettings,
  });
  loadState();
}

async function loadState() {
  applySnapshot(await getState());
  const pending = state.cells.find(
    (cell) => cell.status === "waiting_for_input" && cell.interaction
  );
  if (pending) revealInteraction(pending.id);
}

function handleCell(cell) {
  if (!cell || isGeneratedCell(cell)) return;
  const normalized = normalizeCells([cell])[0];
  if (!normalized) return;
  const index = state.cells.findIndex((item) => item.id === normalized.id);
  if (index === -1) {
    if (normalized.kind) state.cells.push(normalized);
  } else {
    state.cells[index] = { ...state.cells[index], ...normalized };
  }
  renderCellsOnly();
  refreshStatusBar();
  if (normalized.status === "waiting_for_input" && normalized.interaction) {
    revealInteraction(normalized.id);
  }
}

function handleTrace(data) {
  if (!data) return;
  applyTrace(data);
}

function handleJob(event) {
  if (!event) return;
  const job = upsertJob(event);
  if (!job) return;
  const node = document.querySelector('[data-job-id="' + job.job_id + '"]');
  if (!node) {
    renderCellsOnly();
    return;
  }
  updateJobNode(node, job);
}

function handleMap(payload) {
  if (!payload) return;
  state.map_project = payload.project != null ? payload.project : null;
  syncMap();
}

function handleFiles(payload) {
  if (!payload) return;
  setFiles(payload.files || []);
  renderDataOnly();
}

function handleSettings(payload) {
  if (!payload || !payload.settings) return;
  setState({ settings: payload.settings });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
