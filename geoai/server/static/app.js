/* Geo-AI frontend: state + REST, map iframe bridge, SSE subscription. */

"use strict";

const state = {
  active_workspace: null,
  workspaces: [],
  cells: [],
  map_project: null,
  map_app_url: null,
  files: [],
  selected_tab: "Cells",
  settings: { model: "", keep_messages: 24, theme: "light", dangerous_mode: false },
};

// -- map bridge state ------------------------------------------------------

let mapIframe = null;
let mapReady = false;
let lastRemoteProject = null;
let mapSeq = 0;

// -- DOM references --------------------------------------------------------

const appEl = document.getElementById("app");

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2), v);
      } else if (v !== null && v !== undefined) {
        node.setAttribute(k, v);
      }
    }
  }
  if (children != null) {
    for (const c of Array.isArray(children) ? children : [children]) {
      if (c == null) continue;
      node.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
  }
  return node;
}

// -- helpers ---------------------------------------------------------------

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function inlineMarkdown(s) {
  let t = escapeHtml(s);
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t;
}

function renderMarkdown(src) {
  const lines = String(src).split("\n");
  let html = "";
  let inCode = false;
  let codeBuf = [];
  let listBuf = [];

  const flushList = () => {
    if (listBuf.length) {
      html += "<ul>" + listBuf.map((li) => "<li>" + li + "</li>").join("") + "</ul>";
      listBuf = [];
    }
  };

  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inCode) {
        html += "<pre><code>" + escapeHtml(codeBuf.join("\n")) + "</code></pre>";
        codeBuf = [];
        inCode = false;
      } else {
        flushList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeBuf.push(line);
      continue;
    }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      listBuf.push(inlineMarkdown(li[1]));
      continue;
    }
    flushList();
    if (/^######\s/.test(line)) html += "<h6>" + inlineMarkdown(line.replace(/^######\s/, "")) + "</h6>";
    else if (/^#####\s/.test(line)) html += "<h5>" + inlineMarkdown(line.replace(/^#####\s/, "")) + "</h5>";
    else if (/^####\s/.test(line)) html += "<h4>" + inlineMarkdown(line.replace(/^####\s/, "")) + "</h4>";
    else if (/^###\s/.test(line)) html += "<h3>" + inlineMarkdown(line.replace(/^###\s/, "")) + "</h3>";
    else if (/^##\s/.test(line)) html += "<h2>" + inlineMarkdown(line.replace(/^##\s/, "")) + "</h2>";
    else if (/^#\s/.test(line)) html += "<h1>" + inlineMarkdown(line.replace(/^#\s/, "")) + "</h1>";
    else if (/^>\s/.test(line)) html += "<blockquote>" + inlineMarkdown(line.replace(/^>\s/, "")) + "</blockquote>";
    else if (line.trim() !== "") html += "<p>" + inlineMarkdown(line) + "</p>";
  }
  flushList();
  if (inCode) {
    html += "<pre><code>" + escapeHtml(codeBuf.join("\n")) + "</code></pre>";
  }
  return html;
}

function cellOutputText(cell) {
  const parts = [];
  for (const o of cell.outputs || []) {
    if (o.output_type === "stream") {
      if (o.text) parts.push(o.text);
    } else if (o.output_type === "error") {
      if (o.ename || o.evalue) parts.push((o.ename ? o.ename + ": " : "") + (o.evalue || ""));
      if (o.traceback && o.traceback.length) parts.push(o.traceback.join("\n"));
    }
  }
  return parts.join("\n");
}

function canonical(v) {
  if (Array.isArray(v)) return v.map(canonical);
  if (v && typeof v === "object") {
    const out = {};
    for (const k of Object.keys(v).sort()) out[k] = canonical(v[k]);
    return out;
  }
  return v;
}

function sameProject(a, b) {
  if (a == null || b == null) return a === b;
  return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}

function toast(msg) {
  let t = document.getElementById("toast");
  if (!t) {
    t = el("div", { id: "toast" });
    document.body.append(t);
  }
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.style.display = "none"), 4000);
}

// -- REST ------------------------------------------------------------------

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      if (j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json();
}

async function loadState() {
  const snap = await api("GET", "/api/state");
  applySnapshot(snap);
  render();
}

function applySnapshot(snap) {
  state.active_workspace = snap.active_workspace;
  state.workspaces = snap.workspaces || [];
  state.cells = snap.cells || [];
  state.map_project = snap.map_project;
  state.map_app_url = snap.map_app_url;
  state.files = snap.files || [];
  state.settings = snap.settings || { model: "", keep_messages: 24, theme: "light", dangerous_mode: false };
  applyTheme();
}

function applyTheme() {
  const theme = (state.settings && state.settings.theme) || "light";
  document.documentElement.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
}

async function postThenRender(method, url, body) {
  try {
    const snap = await api(method, url, body);
    applySnapshot(snap);
    render();
    syncMap();
  } catch (e) {
    toast(e.message || String(e));
  }
}

// -- map bridge ------------------------------------------------------------

function mapOrigin() {
  if (!state.map_app_url) return null;
  try {
    return new URL(state.map_app_url).origin;
  } catch (_) {
    return null;
  }
}

function postProject() {
  if (!mapIframe || !mapIframe.contentWindow || !mapReady) return;
  if (!state.map_project) return;
  mapSeq += 1;
  mapIframe.contentWindow.postMessage(
    { type: "geolibre:load-project", seq: mapSeq, project: state.map_project },
    mapOrigin()
  );
}

function syncMap() {
  if (!mapIframe || !mapReady) return;
  if (!state.map_project) return;
  if (lastRemoteProject && sameProject(state.map_project, lastRemoteProject)) return;
  postProject();
}

function attachBridge() {
  window.addEventListener("message", (e) => {
    const origin = mapOrigin();
    if (!origin || e.origin !== origin) return;
    if (!mapIframe || e.source !== mapIframe.contentWindow) return;
    const data = e.data;
    if (!data || typeof data !== "object") return;

    if (data.type === "geolibre:ready") {
      mapReady = true;
      postProject();
    } else if (data.type === "geolibre:state") {
      lastRemoteProject = data.project;
      // Persist app-initiated edits (pan/zoom/layer edits in the iframe).
      api("POST", "/api/map/project", { project: data.project }).catch(() => {});
    } else if (data.type === "geolibre:error") {
      const err = document.getElementById("map-error");
      if (err) {
        err.style.display = "block";
        err.textContent = "Map error: " + (data.message || "");
      }
    }
  });
}

function ensureMap() {
  if (!state.map_app_url) return;
  const panel = document.getElementById("map-panel");
  if (!panel) return;
  if (mapIframe && mapIframe.isConnected) return;
  if (!mapIframe) {
    mapIframe = el("iframe", {
      src: state.map_app_url + "index.html?embed=1&theme=light&layout=embed",
      allow: "fullscreen",
    });
  }
  panel.append(mapIframe);
}

// -- render ----------------------------------------------------------------

function render() {
  const menubar = document.getElementById("menubar");
  if (!menubar) {
    // First render: build the full shell. The map panel (and its iframe) is
    // built once and never rebuilt, so the map does not blink/vanish on later
    // cell or workspace mutations.
    appEl.replaceChildren();
    appEl.append(renderMenubar(), renderMapPanel(), renderSidePanel());
    ensureMap();
    return;
  }
  menubar.replaceWith(renderMenubar());
  const side = document.getElementById("side-panel");
  if (side) side.replaceWith(renderSidePanel());
  ensureMap();
}

function renderMenubar() {
  const bar = el("div", { id: "menubar" });
  bar.append(
    el("div", { class: "brand" }, [
      el("span", { class: "brand-mark", text: "◈" }),
      el("span", { class: "brand-name", text: "Geo-AI" }),
    ])
  );
  const fileMenu = el("div", { class: "menu" });
  const fileBtn = el("button", { text: "File", onclick: () => toggleMenu(fileMenu) });
  const fileItems = el("div", { class: "menu-items" });
  fileItems.append(
    menuItem("New…", () => openNewDialog()),
    menuItem("Open…", () => openOpenDialog()),
    menuItem("Save", () => doSave()),
    menuItem("Close", () => postThenRender("POST", "/api/workspace/close")),
    el("div", { class: "separator" }),
    menuItem("Settings…", () => openSettingsDialog()),
    el("div", { class: "separator" }),
    menuItem("Exit", () => doExit())
  );
  fileMenu.append(fileBtn, fileItems);

  const cellMenu = el("div", { class: "menu" });
  const cellBtn = el("button", { text: "Cell", onclick: () => toggleMenu(cellMenu) });
  const cellItems = el("div", { class: "menu-items" });
  cellItems.append(
    menuItem("Add Markdown Cell", () => addCell("markdown")),
    menuItem("Add Python Cell", () => addCell("python")),
    menuItem("Add Prompt Cell", () => addCell("prompt")),
    el("div", { class: "separator" }),
    menuItem("Run All", () => doRunAll())
  );
  cellMenu.append(cellBtn, cellItems);

  bar.append(fileMenu, cellMenu);
  const right = el("div", { class: "menubar-right" });
  if (state.active_workspace) {
    right.append(el("div", { id: "ws-chip", text: state.active_workspace }));
  }
  right.append(dangerToggle());
  bar.append(right);
  return bar;
}

function menuItem(label, onclick) {
  return el("button", { text: label, onclick });
}

const ICON_SAFE =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>';

const ICON_DANGER =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" stroke="none">' +
  '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>';

function dangerToggle() {
  const on = !!(state.settings && state.settings.dangerous_mode);
  const btn = el("button", {
    id: "danger-toggle",
    class: "danger-toggle" + (on ? " on" : ""),
    title: on
      ? "Dangerous mode ON — run_python may execute arbitrary commands"
      : "Dangerous mode OFF — run_python is sandboxed",
    "aria-pressed": String(on),
    onclick: () => toggleDangerous(!on),
  });
  btn.innerHTML = on ? ICON_DANGER : ICON_SAFE;
  return btn;
}

async function toggleDangerous(next) {
  try {
    const settings = await api("PUT", "/api/settings", { dangerous_mode: next });
    state.settings = settings;
    render();
    toast(next ? "Dangerous mode enabled" : "Dangerous mode disabled");
  } catch (e) {
    toast(e.message || String(e));
  }
}


function toggleMenu(menu) {
  const wasOpen = menu.classList.contains("open");
  closeAllMenus();
  if (!wasOpen) menu.classList.add("open");
}

function closeAllMenus() {
  document.querySelectorAll(".menu.open").forEach((m) => m.classList.remove("open"));
}

function renderMapPanel() {
  const panel = el("div", { id: "map-panel" });
  const err = el("div", { id: "map-error" });
  panel.append(err);
  return panel;
}

function renderSidePanel() {
  const panel = el("div", { id: "side-panel" });
  const tabbar = el("div", { class: "tabbar" });
  tabbar.append(
    tabButton("Cells"),
    tabButton("Data")
  );
  const content = el("div", { id: "tab-content" });
  content.append(state.selected_tab === "Cells" ? renderCellsTab() : renderDataTab());
  panel.append(tabbar, content, renderStatusBar());
  return panel;
}

function tabButton(name) {
  return el("button", {
    text: name,
    class: name === state.selected_tab ? "active" : "",
    onclick: () => {
      state.selected_tab = name;
      render();
    },
  });
}

function renderCellsTab() {
  const wrap = el("div", {});

  if (!state.active_workspace) {
    wrap.append(
      el("div", { class: "empty-hint", text: "No workspace open — File → New or Open" })
    );
    return wrap;
  }

  const addRow = el("div", { class: "add-cell-row" });
  addRow.append(
    el("button", { text: "+ Markdown", onclick: () => addCell("markdown") }),
    el("button", { text: "+ Python", onclick: () => addCell("python") }),
    el("button", { text: "+ Prompt", onclick: () => addCell("prompt") })
  );
  wrap.append(addRow);

  if (!state.cells.length) {
    wrap.append(el("div", { class: "empty-hint", text: "No cells yet — add a cell above." }));
    return wrap;
  }

  for (const cell of state.cells) {
    wrap.append(renderCell(cell));
  }
  return wrap;
}

function renderCell(cell) {
  const kind = cell.kind;
  const box = el("div", { class: "cell", "data-cell-id": cell.id });

  const header = el("div", { class: "cell-header" });
  if (kind !== "markdown") {
    if (cell.status === "running" && kind === "prompt") {
      header.append(
        el("button", {
          class: "run-btn stop",
          text: "■",
          title: "Stop",
          onclick: () => stopCell(cell.id),
        })
      );
    } else {
      header.append(
        el("button", {
          class: "run-btn",
          text: "▶",
          title: "Run",
          onclick: () => runCell(cell.id),
        })
      );
    }
  }
  header.append(
    el("span", {
      class: "counter",
      text: "In[" + (cell.execution_count != null ? cell.execution_count : " ") + "]",
    })
  );
  header.append(el("span", { class: "badge", text: kind === "markdown" ? "md" : kind === "prompt" ? "prompt" : "py" }));
  header.append(el("span", { class: "spacer" }));

  if (kind === "markdown") {
    header.append(
      el("button", { class: "icon", text: "edit", title: "Edit", onclick: () => editMarkdown(cell) }),
      el("button", { class: "icon", text: "✕", title: "Delete", onclick: () => deleteCell(cell.id) })
    );
  } else {
    header.append(
      el("button", { class: "icon", text: "↑", title: "Move up", onclick: () => moveCell(cell.id, -1) }),
      el("button", { class: "icon", text: "↓", title: "Move down", onclick: () => moveCell(cell.id, 1) }),
      el("button", { class: "icon", text: "✕", title: "Delete", onclick: () => deleteCell(cell.id) })
    );
  }
  box.append(header);

  const body = el("div", { class: "cell-body" });
  if (kind === "markdown") {
    const md = el("div", { class: "markdown" });
    md.innerHTML = renderMarkdown(cell.source);
    body.append(md);
  } else {
    const ta = el("textarea", {
      rows: Math.min(12, Math.max(2, cell.source.split("\n").length)),
    });
    ta.value = cell.source;
    ta.addEventListener("input", () => {
      const idx = state.cells.findIndex((c) => c.id === cell.id);
      if (idx >= 0) state.cells[idx].source = ta.value;
    });
    ta.addEventListener("blur", () => {
      updateCell(cell.id, ta.value);
    });
    body.append(ta);
  }
  box.append(body);

  if (kind !== "markdown") {
    const outRow = el("div", { class: "cell-out" });
    outRow.append(
      el("span", {
        class: "counter",
        text: "Out[" + (cell.execution_count != null ? cell.execution_count : " ") + "]",
      })
    );
    if (cell.status === "running") {
      outRow.append(el("span", { class: "running", text: "running…" }));
    } else if (cell.status === "stopped") {
      outRow.append(el("span", { class: "running stopped", text: "stopped" }));
    }
    if (kind === "prompt" && cell.usage) {
      outRow.append(
        el("span", { class: "usage", text: usageLabel(cell.usage), title: usageTitle(cell.usage) })
      );
    }
    box.append(outRow);

    if (kind === "prompt") {
      const trace = el("div", { class: "trace" });
      for (const node of renderTraceSteps(cell.trace || [])) trace.append(node);
      box.append(trace);
    }

    const outBlock = el("div", { class: "cell-out-block" });
    const text = cellOutputText(cell);
    if (text) {
      outBlock.append(el("pre", { class: cell.status === "error" ? "error" : "", text }));
    }
    box.append(outBlock);
  }

  return box;
}

function editMarkdown(cell) {
  const box = document.querySelector('.cell[data-cell-id="' + cell.id + '"]');
  if (!box) return;
  const body = box.querySelector(".cell-body");
  const ta = el("textarea", { rows: Math.min(12, Math.max(2, cell.source.split("\n").length)) });
  ta.value = cell.source;
  ta.addEventListener("blur", () => {
    updateCell(cell.id, ta.value);
  });
  body.replaceChildren(ta);
  ta.focus();
}
// -- agent trace -----------------------------------------------------------

function truncate(s, n) {
  s = String(s == null ? "" : s);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function formatTokens(n) {
  n = Number(n) || 0;
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

function formatCost(c) {
  if (c == null) return null;
  const n = Number(c);
  if (!isFinite(n) || n === 0) return null;
  return "$" + (n < 0.001 ? n.toExponential(1) : n.toFixed(4));
}

function usageLabel(u) {
  const parts = [];
  if (u.input_tokens != null) parts.push("↑" + formatTokens(u.input_tokens));
  if (u.output_tokens != null) parts.push("↓" + formatTokens(u.output_tokens));
  const cost = formatCost(u.cost);
  if (cost) parts.push(cost);
  return parts.join(" ");
}

function usageTitle(u) {
  const parts = [];
  if (u.requests != null) parts.push(u.requests + " request" + (u.requests === 1 ? "" : "s"));
  if (u.tool_calls != null) parts.push(u.tool_calls + " tool call" + (u.tool_calls === 1 ? "" : "s"));
  if (u.total_tokens != null) parts.push(u.total_tokens + " total tokens");
  if (u.cache_read_tokens) parts.push(u.cache_read_tokens + " cached read tokens");
  return parts.join(" · ");
}

function renderTraceSteps(trace) {
  const steps = trace || [];
  const nodes = [];
  const plan = planFromTrace(steps);
  if (plan) nodes.push(planNode(plan));
  for (const group of groupTraceSteps(steps)) {
    if (group.type === "text") {
      nodes.push(el("div", { class: "trace-step trace-text", text: group.content }));
    } else if (group.type === "tool") {
      nodes.push(toolStepNode(group));
    } else if (group.type === "usage") {
      nodes.push(usageNode(group.usage));
    }
  }
  return nodes;
}

function isCodeTool(name) {
  return name === "run_python";
}

function prettyValue(value) {
  if (typeof value === "string") {
    const t = value.trim();
    if ((t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]"))) {
      try {
        return JSON.stringify(JSON.parse(value), null, 2);
      } catch (_) {
        /* not valid JSON — fall through to raw */
      }
    }
    return value;
  }
  if (value == null) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch (_) {
    return String(value);
  }
}

function compactPreview(value) {
  let s;
  if (typeof value === "string") s = value;
  else if (value == null) s = "";
  else {
    try {
      s = JSON.stringify(value);
    } catch (_) {
      s = String(value);
    }
  }
  return truncate(String(s).replace(/\s+/g, " ").trim(), 100);
}

function extractCode(args) {
  let obj = args;
  if (typeof obj === "string") {
    try {
      obj = JSON.parse(obj);
    } catch (_) {
      return obj;
    }
  }
  if (obj && typeof obj === "object") {
    if (obj.code != null) return obj.code;
    if (obj.source != null) return obj.source;
  }
  return obj;
}

function codeBlock(content) {
  const pre = el("pre", { class: "trace-code" });
  pre.append(el("code", { text: content == null ? "" : String(content) }));
  return pre;
}

// -- planning ---------------------------------------------------------------

function parsePlanItems(args) {
  let obj = args;
  if (typeof obj === "string") {
    try {
      obj = JSON.parse(obj);
    } catch (_) {
      return [];
    }
  }
  if (obj && typeof obj === "object" && Array.isArray(obj.items)) return obj.items;
  return [];
}

function planFromTrace(steps) {
  let items = null;
  for (const step of steps) {
    if (step.type === "tool_call" && step.name === "write_plan") {
      items = parsePlanItems(step.args);
    }
  }
  return items && items.length ? items : null;
}

function statusOf(item) {
  return (item && item.status) || "pending";
}

function planStatusIcon(status) {
  if (status === "completed") return "✓";
  if (status === "in_progress") return "●";
  if (status === "cancelled") return "×";
  if (status === "blocked") return "!";
  return "○";
}

function planItemNode(item) {
  const status = statusOf(item);
  const li = el("li", { class: "plan-item plan-" + status });
  li.append(el("span", { class: "plan-item-icon", text: planStatusIcon(status) }));
  const text = status === "in_progress" && item.active_form ? item.active_form : item.content;
  li.append(el("span", { class: "plan-item-text", text: text || item.content || "" }));
  return li;
}

function planList(items) {
  const list = el("ol", { class: "plan-list" });
  for (const item of items || []) list.append(planItemNode(item));
  return list;
}

function planNode(items) {
  const total = items.length;
  const done = items.filter((i) => {
    const s = statusOf(i);
    return s === "completed" || s === "cancelled";
  }).length;
  const details = el("details", { class: "trace-step trace-plan", open: true });
  const summary = el("summary", {});
  summary.append(
    el("span", { class: "trace-icon", text: "☑" }),
    el("span", { class: "trace-name", text: "Plan" })
  );
  summary.append(el("span", { class: "trace-preview", text: done + "/" + total + " done" }));
  details.append(summary);
  const body = el("div", { class: "trace-body" });
  const bar = el("div", { class: "plan-progress" });
  const fill = el("div", { class: "plan-progress-fill" });
  fill.style.width = (total ? Math.round((done / total) * 100) : 0) + "%";
  bar.append(fill);
  body.append(bar);
  body.append(planList(items));
  details.append(body);
  return details;
}

function usageNode(usage) {
  const node = el("div", { class: "trace-step trace-usage" });
  node.append(
    el("span", { class: "trace-icon", text: "Σ" }),
    el("span", { class: "trace-name", text: "Usage" })
  );
  const u = usage || {};
  node.append(el("span", { class: "trace-preview", text: usageLabel(u), title: usageTitle(u) }));
  return node;
}

function toolPreview(name, call, result, isCode) {
  if (name === "write_plan" && call) {
    const items = parsePlanItems(call.args);
    const done = items.filter((i) => {
      const s = statusOf(i);
      return s === "completed" || s === "cancelled";
    }).length;
    const active = items.filter((i) => statusOf(i) === "in_progress").length;
    return items.length + " steps · " + done + " done" + (active ? " · " + active + " active" : "");
  }
  if (call) return compactPreview(isCode ? extractCode(call.args) : call.args);
  if (result) return compactPreview(result.content);
  return "";
}

function toolStepNode(group) {
  const call = group.call;
  const result = group.result;
  const name = (call && call.name) || (result && result.name) || "";
  const isCode = isCodeTool(name);
  const details = el("details", {
    class: "trace-step tool-call" + (result ? "" : " pending"),
  });
  const summary = el("summary", {});
  summary.append(
    el("span", { class: "trace-icon", text: "→" }),
    el("span", { class: "trace-name", text: name || "" })
  );
  const preview = toolPreview(name, call, result, isCode);
  if (preview) summary.append(el("span", { class: "trace-preview", text: preview }));
  details.append(summary);
  const body = el("div", { class: "trace-body" });
  if (call) {
    body.append(el("div", { class: "trace-io-label", text: "Input" }));
    body.append(codeBlock(isCode ? extractCode(call.args) : prettyValue(call.args)));
  }
  if (result) {
    body.append(el("div", { class: "trace-io-label", text: "Output" }));
    body.append(codeBlock(prettyValue(result.content)));
  }
  details.append(body);
  return details;
}

function toolResultNode(step) {
  const name = step.name || "";
  const details = el("details", { class: "trace-step tool-result" });
  const summary = el("summary", {});
  summary.append(
    el("span", { class: "trace-icon", text: "←" }),
    el("span", { class: "trace-name", text: name || "" })
  );
  summary.append(el("span", { class: "trace-preview", text: compactPreview(step.content) }));
  details.append(summary);
  const body = el("div", { class: "trace-body" });
  body.append(codeBlock(prettyValue(step.content)));
  details.append(body);
  return details;
}

function groupTraceSteps(trace) {
  const steps = trace || [];
  const groups = [];
  let textBuf = null;
  const flush = () => {
    if (textBuf !== null) {
      groups.push({ type: "text", content: textBuf });
      textBuf = null;
    }
  };
  const pending = new Map();
  let fallbackSeq = 0;

  for (const step of steps) {
    if (step.type === "text") {
      flush();
      textBuf = step.content || "";
    } else if (step.type === "text_delta") {
      if (textBuf === null) textBuf = "";
      textBuf += step.content || "";
    } else if (step.type === "tool_call") {
      flush();
      const group = { type: "tool", call: step, result: null };
      groups.push(group);
      pending.set(step.tool_call_id || "seq:" + fallbackSeq++, group);
    } else if (step.type === "tool_result") {
      flush();
      let group = null;
      if (step.tool_call_id && pending.has(step.tool_call_id)) {
        group = pending.get(step.tool_call_id);
        pending.delete(step.tool_call_id);
      } else {
        for (const [key, p] of pending) {
          if ((p.call && p.call.name) === (step.name || "")) {
            group = p;
            pending.delete(key);
            break;
          }
        }
      }
      if (group) {
        group.result = step;
      } else {
        groups.push({ type: "tool", call: null, result: step });
      }
    } else if (step.type === "usage") {
      flush();
      groups.push({ type: "usage", usage: step.usage });
    } else {
      flush();
    }
  }
  flush();
  return groups;
}

function attachToolResult(node, result) {
  node.classList.remove("pending");
  const body = node.querySelector(".trace-body");
  if (body) {
    body.append(el("div", { class: "trace-io-label", text: "Output" }));
    body.append(codeBlock(prettyValue(result.content)));
  }
}

function updatePlanNode(container, items) {
  if (!items || !items.length) return;
  const node = planNode(items);
  const existing = container.querySelector(".trace-plan");
  if (existing) existing.replaceWith(node);
  else container.prepend(node);
}

function appendTraceStep(container, step) {
  if (step.type === "text_delta") {
    const last = container.lastElementChild;
    if (last && last.classList.contains("trace-text")) {
      last.textContent += step.content || "";
      container.scrollTop = container.scrollHeight;
      return;
    }
    container.append(el("div", { class: "trace-step trace-text", text: step.content || "" }));
  } else if (step.type === "text") {
    container.append(el("div", { class: "trace-step trace-text", text: step.content || "" }));
  } else if (step.type === "tool_call") {
    const node = toolStepNode({ call: step, result: null });
    container.append(node);
    const pending = (container._pending || (container._pending = new Map()));
    pending.set(step.tool_call_id || "seq:" + pending.size, { node, step });
    if (step.name === "write_plan") updatePlanNode(container, parsePlanItems(step.args));
  } else if (step.type === "tool_result") {
    const pending = (container._pending || (container._pending = new Map()));
    let entry = null;
    if (step.tool_call_id && pending.has(step.tool_call_id)) {
      entry = pending.get(step.tool_call_id);
      pending.delete(step.tool_call_id);
    } else {
      for (const [key, p] of pending) {
        if ((p.step.name || "") === (step.name || "")) {
          entry = p;
          pending.delete(key);
          break;
        }
      }
    }
    if (entry) {
      attachToolResult(entry.node, step);
    } else {
      container.append(toolResultNode(step));
    }
  } else if (step.type === "usage") {
    container.append(usageNode(step.usage));
  }
  container.scrollTop = container.scrollHeight;
}

function applyTrace(data) {
  const idx = state.cells.findIndex((c) => c.id === data.id);
  if (idx === -1) return;
  if (!Array.isArray(state.cells[idx].trace)) state.cells[idx].trace = [];
  state.cells[idx].trace.push(data.step);
  const container = document.querySelector('.cell[data-cell-id="' + data.id + '"] .trace');
  if (!container) return;
  appendTraceStep(container, data.step);
}
function buildFileTree(files) {
  const root = { name: "", isDir: true, children: new Map() };
  for (const f of files) {
    const parts = String(f).split("/");
    let node = root;
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      const isDir = i < parts.length - 1;
      let child = node.children.get(part);
      if (!child) {
        child = { name: part, isDir, children: new Map() };
        node.children.set(part, child);
      }
      node = child;
    }
  }
  return root;
}

function sortedChildren(node) {
  return [...node.children.values()].sort((a, b) => {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

function renderFileTree(files) {
  const ul = el("ul", { class: "file-tree" });
  for (const node of sortedChildren(buildFileTree(files))) {
    ul.append(renderTreeNode(node));
  }
  return ul;
}

function renderTreeNode(node) {
  if (node.isDir) {
    const li = el("li", { class: "tree-dir" });
    const details = el("details", { open: "" });
    const summary = el("summary", {});
    summary.append(el("span", { class: "tree-name", text: node.name }));
    details.append(summary);
    const childUl = el("ul", { class: "file-tree" });
    for (const child of sortedChildren(node)) {
      childUl.append(renderTreeNode(child));
    }
    details.append(childUl);
    li.append(details);
    return li;
  }
  const li = el("li", { class: "tree-file" });
  li.append(el("span", { class: "tree-name", text: node.name }));
  return li;
}

function renderDataTab() {
  const wrap = el("div", {});

  if (!state.active_workspace) {
    wrap.append(
      el("div", { class: "empty-hint", text: "No workspace open — File → New or Open" })
    );
    return wrap;
  }

  const localRow = el("div", { class: "import-row" });
  const filesInput = el("input", { type: "file", multiple: "multiple", style: "display:none" });
  const folderInput = el("input", { type: "file", webkitdirectory: "", style: "display:none" });
  filesInput.addEventListener("change", () => importLocal(filesInput));
  folderInput.addEventListener("change", () => importLocal(folderInput));
  localRow.append(
    filesInput,
    folderInput,
    el("button", { text: "Import files…", onclick: () => filesInput.click() }),
    el("button", { text: "Import folder…", onclick: () => folderInput.click() })
  );
  wrap.append(localRow);

  const urlRow = el("div", { class: "import-row" });
  const urlInput = el("input", { type: "url", placeholder: "https://example.com/file.tif" });
  urlRow.append(urlInput, el("button", { text: "Download", onclick: () => importUrl(urlInput) }));
  wrap.append(urlRow);

  if (!state.files.length) {
    wrap.append(el("div", { class: "empty-hint", text: "(no files yet)" }));
  } else {
    wrap.append(renderFileTree(state.files));
  }

  return wrap;
}

// -- cell actions ----------------------------------------------------------

async function addCell(kind) {
  if (!state.active_workspace) {
    toast("No workspace open");
    return;
  }
  await postThenRender("POST", "/api/cells", { kind, source: "", index: null });
}

async function updateCell(cellId, source) {
  await postThenRender("PUT", "/api/cells/" + cellId, { source });
}

async function deleteCell(cellId) {
  await postThenRender("DELETE", "/api/cells/" + cellId);
}

async function moveCell(cellId, dir) {
  const idx = state.cells.findIndex((c) => c.id === cellId);
  if (idx < 0) return;
  const target = idx + dir;
  if (target < 0 || target >= state.cells.length) return;
  await postThenRender("POST", "/api/cells/" + cellId + "/move", { index: target });
}

async function runCell(cellId) {
  try {
    await api("POST", "/api/cells/" + cellId + "/run");
  } catch (e) {
    toast(e.message || String(e));
  }
}


async function stopCell(cellId) {
  try {
    await api("POST", "/api/cells/" + cellId + "/stop");
  } catch (e) {
    toast(e.message || String(e));
  }
}
async function doRunAll() {
  try {
    await api("POST", "/api/run-all");
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function importLocal(fileInput) {
  if (!fileInput.files.length) {
    toast("Choose a file or folder first");
    return;
  }
  const fd = new FormData();
  for (const f of fileInput.files) {
    fd.append("files", f, f.webkitRelativePath || f.name);
  }
  try {
    const res = await fetch("/api/import/local", { method: "POST", body: fd });
    if (!res.ok) throw new Error("import failed (" + res.status + ")");
    const result = await res.json();
    const names = result.imported || [];
    fileInput.value = "";
    await loadState();
    toast("Imported " + names.length + " file(s)");
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function importUrl(urlInput) {
  const url = urlInput.value.trim();
  if (!url) {
    toast("Enter a URL first");
    return;
  }
  await postThenRender("POST", "/api/import/url", { url, filename: null });
}

async function doSave() {
  if (!state.active_workspace) {
    toast("No workspace open");
    return;
  }
  try {
    await api("POST", "/api/workspace/save");
    toast("Saved");
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function doExit() {
  try {
    await api("POST", "/api/workspace/close");
  } catch (e) {
    /* ignore — server stays up */
  }
  state.active_workspace = null;
  state.workspace = null;
  state.cells = [];
  state.files = [];
  render();
}

// -- dialogs ---------------------------------------------------------------

let dialogCleanup = null;

function openDialog(buildContent) {
  closeAllMenus();
  const overlay = el("div", { id: "overlay", class: "open" });
  const dialog = el("div", { id: "dialog" });
  overlay.append(dialog);
  document.body.append(overlay);
  dialogCleanup = () => overlay.remove();
  buildContent(dialog);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeDialog();
  });
}

function closeDialog() {
  if (dialogCleanup) {
    dialogCleanup();
    dialogCleanup = null;
  }
}

function dialogActions(dialog, primary, onPrimary) {
  const actions = el("div", { class: "dialog-actions" });
  actions.append(el("button", { text: "Cancel", onclick: closeDialog }));
  if (primary) {
    actions.append(el("button", { class: "primary", text: primary, onclick: onPrimary }));
  }
  dialog.append(actions);
}

function openNewDialog() {
  openDialog((dialog) => {
    dialog.append(el("h3", { text: "New workspace" }));
    const input = el("input", { type: "text", placeholder: "workspace name" });
    dialog.append(input);
    dialogActions(dialog, "Create", async () => {
      const name = input.value.trim();
      if (!name) return;
      closeDialog();
      await postThenRender("POST", "/api/workspace/new", { name });
    });
    input.focus();
  });
}

function openOpenDialog() {
  openDialog((dialog) => {
    dialog.append(el("h3", { text: "Open workspace" }));
    const list = el("ul", { id: "ws-list" });
    if (!state.workspaces.length) {
      list.append(el("li", { text: "(no workspaces yet)" }));
    }
    for (const name of state.workspaces) {
      list.append(
        el("li", {
          text: name,
          onclick: async () => {
            closeDialog();
            await postThenRender("POST", "/api/workspace/open", { name });
          },
        })
      );
    }
    dialog.append(list);
    dialogActions(dialog, null, null);
  });
}

function openSettingsDialog() {
  openDialog((dialog) => {
    dialog.append(el("h3", { text: "Settings" }));

    const row = (label, hint, input) => {
      const r = el("div", { class: "settings-row" });
      const labels = el("div", { class: "settings-labels" });
      labels.append(el("label", { text: label }));
      if (hint) labels.append(el("span", { class: "settings-hint", text: hint }));
      r.append(labels, input);
      return r;
    };

    const modelInput = el("input", {
      type: "text",
      value: state.settings.model || "",
      placeholder: "openai:gpt-4o",
    });
    const keepInput = el("input", {
      type: "number",
      min: "0",
      step: "1",
      value: state.settings.keep_messages != null ? state.settings.keep_messages : 24,
    });
    const themeSelect = el("select", {});
    themeSelect.append(
      el("option", { value: "light", text: "Light" }),
      el("option", { value: "dark", text: "Dark" })
    );
    themeSelect.value = state.settings.theme === "dark" ? "dark" : "light";

    dialog.append(
      row("Model", "e.g. openai:gpt-4o, anthropic:claude-sonnet-4-5", modelInput),
      row("Recent messages to keep", "prior prompt-cell messages replayed into a new cell", keepInput),
      row("Theme", "app shell appearance", themeSelect)
    );

    dialogActions(dialog, "Save", async () => {
      try {
        const settings = await api("PUT", "/api/settings", {
          model: modelInput.value.trim(),
          keep_messages: Number(keepInput.value) || 0,
          theme: themeSelect.value,
        });
        state.settings = settings;
        applyTheme();
        closeDialog();
        toast("Settings saved");
        render();
      } catch (e) {
        toast(e.message || String(e));
      }
    });
  });
}

function aggregateUsage() {
  let input = 0, output = 0, requests = 0, toolCalls = 0, cost = 0, cacheRead = 0, has = false;
  for (const c of state.cells || []) {
    const u = c.usage;
    if (!u) continue;
    has = true;
    input += u.input_tokens || 0;
    output += u.output_tokens || 0;
    requests += u.requests || 0;
    toolCalls += u.tool_calls || 0;
    if (u.cost != null) cost += Number(u.cost) || 0;
    cacheRead += u.cache_read_tokens || 0;
  }
  return { input, output, requests, toolCalls, cost, cacheRead, has };
}

function renderStatusBar() {
  const bar = el("div", { id: "status-bar" });
  const ws = el("span", { class: "status-ws", text: state.active_workspace || "No workspace" });
  bar.append(ws);
  const u = aggregateUsage();
  if (u.has) {
    const stats = el("div", { class: "status-stats" });
    stats.append(
      el("span", { class: "stat", title: "Input tokens", text: "↑ " + formatTokens(u.input) }),
      el("span", { class: "stat", title: "Output tokens", text: "↓ " + formatTokens(u.output) }),
      el("span", { class: "stat", title: "Requests", text: u.requests + " req" })
    );
    const cost = formatCost(u.cost);
    if (cost) stats.append(el("span", { class: "stat cost", text: cost }));
    const ratio = u.input ? Math.round((u.cacheRead / u.input) * 100) : 0;
    stats.append(el("span", { class: "stat", title: "Prompt cache hit ratio (DeepSeek)", text: ratio + "% cached" }));
    bar.append(stats);
  }
  return bar;
}

function refreshStatusBar() {
  const bar = document.getElementById("status-bar");
  if (bar && bar.parentElement) {
    bar.replaceWith(renderStatusBar());
  }
}

// -- SSE -------------------------------------------------------------------

function connectSSE() {
  const es = new EventSource("/api/events");
  es.addEventListener("cell", (e) => {
    const data = JSON.parse(e.data);
    const idx = state.cells.findIndex((c) => c.id === data.id);
    if (idx === -1) {
      if (data.kind) state.cells.push(data);
    } else {
      state.cells[idx] = { ...state.cells[idx], ...data };
    }
    renderCellsOnly();
    refreshStatusBar();
  });
  es.addEventListener("trace", (e) => {
    applyTrace(JSON.parse(e.data));
  });
  es.addEventListener("map", (e) => {
    const data = JSON.parse(e.data);
    state.map_project = data.project;
    syncMap();
  });
  es.addEventListener("files", (e) => {
    const data = JSON.parse(e.data);
    state.files = data.files || [];
    renderDataOnly();
  });
  es.addEventListener("settings", (e) => {
    const data = JSON.parse(e.data);
    if (data.settings) {
      state.settings = data.settings;
      applyTheme();
      render();
    }
  });
}

function renderCellsOnly() {
  const content = document.getElementById("tab-content");
  if (content && state.selected_tab === "Cells") {
    content.replaceChildren(renderCellsTab());
  }
}

function renderDataOnly() {
  const content = document.getElementById("tab-content");
  if (content && state.selected_tab === "Data") {
    content.replaceChildren(renderDataTab());
  }
}

// -- boot ------------------------------------------------------------------

document.addEventListener("click", (e) => {
  if (!e.target.closest(".menu")) closeAllMenus();
});

attachBridge();
connectSSE();
loadState();
