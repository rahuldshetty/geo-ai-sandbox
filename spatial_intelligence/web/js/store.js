/* Single mutable application store shared by every frontend module.

`state` is the one mutable object; `setState`/`applySnapshot` write it and
notify subscribers. `upsertJob` is a pure write used by the SSE path, which
updates its own region of the DOM instead of repainting the whole shell.
*/

"use strict";

const DEFAULT_SETTINGS = {
  model: "",
  theme: "light",
  dangerous_mode: false,
  max_retries: 5,
  record_agent_steps: true,
};

export const state = {
  active_workspace: null,
  workspaces: [],
  cells: [],
  map_project: null,
  map_app_url: null,
  files: [],
  jobs: [],
  selected_tab: "Cells",
  settings: { ...DEFAULT_SETTINGS },
};

const listeners = new Set();

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function notify() {
  for (const listener of [...listeners]) listener(state);
}

export function setState(patch) {
  if (!patch) return state;
  const next = { ...patch };
  if ("cells" in next) next.cells = normalizeCells(next.cells);
  if ("files" in next) next.files = normalizeFilePaths(next.files);
  if ("jobs" in next) next.jobs = normalizeJobs(next.jobs);
  Object.assign(state, next);
  if ("settings" in next) applyTheme();
  notify();
  return state;
}

export function applySnapshot(snapshot) {
  const snap = snapshot || {};
  return setState({
    active_workspace: snap.active_workspace != null ? snap.active_workspace : null,
    workspaces: snap.workspaces || [],
    cells: snap.cells || [],
    map_project: snap.map_project != null ? snap.map_project : null,
    map_app_url: snap.map_app_url != null ? snap.map_app_url : null,
    files: snap.files || [],
    jobs: snap.jobs || [],
    settings: snap.settings || state.settings,
  });
}

export function isGeneratedCell(cell) {
  const geoai = (cell && cell.metadata && cell.metadata.geoai) || {};
  return Boolean(geoai.generated || geoai.kind === "interaction");
}

export function normalizeCells(cells) {
  return Array.isArray(cells)
    ? cells
        .filter((cell) => cell && typeof cell === "object" && !isGeneratedCell(cell))
        .map((cell) => {
          const normalized = { ...cell };
          if (Object.prototype.hasOwnProperty.call(cell, "source")) {
            normalized.source = cell.source == null ? "" : String(cell.source);
          }
          return normalized;
        })
    : [];
}

export function setFiles(files) {
  state.files = normalizeFilePaths(files);
  return state.files;
}

export function jobsFor(cellId) {
  return (state.jobs || []).filter((job) => job && job.parent_id === cellId);
}

export function upsertJob(job) {
  if (!job) return null;
  const id = job.job_id || job.id;
  if (!id) return null;
  const jobs = (state.jobs || []).slice();
  const index = jobs.findIndex((item) => (item.job_id || item.id) === id);
  const stored = index === -1 ? { ...job, job_id: id } : { ...jobs[index], ...job, job_id: id };
  if (index === -1) jobs.push(stored);
  else jobs[index] = stored;
  state.jobs = jobs;
  return stored;
}

export function usageTotals() {
  let input = 0;
  let output = 0;
  let requests = 0;
  let toolCalls = 0;
  let cost = 0;
  let cacheRead = 0;
  let has = false;
  for (const cell of state.cells || []) {
    const usage = cell.usage;
    if (!usage) continue;
    has = true;
    input += usage.input_tokens || 0;
    output += usage.output_tokens || 0;
    requests += usage.requests || 0;
    toolCalls += usage.tool_calls || 0;
    if (usage.cost != null) cost += Number(usage.cost) || 0;
    cacheRead += usage.cache_read_tokens || 0;
  }
  return { input, output, requests, toolCalls, cost, cacheRead, has };
}

function normalizeFilePaths(files) {
  return Array.isArray(files)
    ? files.filter((path) => typeof path === "string" && path.trim())
    : [];
}

function normalizeJobs(jobs) {
  return Array.isArray(jobs) ? jobs.filter((job) => job && typeof job === "object") : [];
}

function applyTheme() {
  const theme = (state.settings && state.settings.theme) || "light";
  document.documentElement.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
}
