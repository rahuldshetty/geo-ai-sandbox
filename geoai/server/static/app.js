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

  const fileMenu = el("div", { class: "menu" });
  const fileBtn = el("button", { text: "File", onclick: () => toggleMenu(fileMenu) });
  const fileItems = el("div", { class: "menu-items" });
  fileItems.append(
    menuItem("New…", () => openNewDialog()),
    menuItem("Open…", () => openOpenDialog()),
    menuItem("Save", () => doSave()),
    menuItem("Close", () => postThenRender("POST", "/api/workspace/close")),
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
  if (state.active_workspace) {
    bar.append(el("div", { id: "ws-chip", text: state.active_workspace }));
  }
  return bar;
}

function menuItem(label, onclick) {
  return el("button", { text: label, onclick });
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
  panel.append(tabbar, content);
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

function renderTraceSteps(trace) {
  const nodes = [];
  let textBuf = null;
  const flush = () => {
    if (textBuf !== null) {
      nodes.push(el("div", { class: "trace-step trace-text", text: textBuf }));
      textBuf = null;
    }
  };
  for (const step of trace || []) {
    if (step.type === "text") {
      flush();
      textBuf = step.content || "";
    } else if (step.type === "text_delta") {
      if (textBuf === null) textBuf = "";
      textBuf += step.content || "";
    } else {
      flush();
      const node = traceStepNode(step);
      if (node) nodes.push(node);
    }
  }
  flush();
  return nodes;
}

function traceStepNode(step) {
  if (step.type === "tool_call") {
    return el("div", { class: "trace-step tool-call" }, [
      el("span", { class: "trace-icon", text: "→" }),
      el("span", { class: "trace-name", text: step.name || "" }),
      el("code", { class: "trace-args", text: truncate(step.args || "", 240) }),
    ]);
  }
  if (step.type === "tool_result") {
    return el("div", { class: "trace-step tool-result" }, [
      el("span", { class: "trace-icon", text: "←" }),
      el("span", { class: "trace-name", text: step.name || "" }),
      el("span", { class: "trace-result", text: truncate(step.content || "", 320) }),
    ]);
  }
  return null;
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
  } else {
    const node = traceStepNode(step);
    if (node) container.append(node);
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
  actions.append(
    el("button", { text: "Cancel", onclick: closeDialog }),
    el("button", { class: "primary", text: primary, onclick: onPrimary })
  );
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
    dialogActions(dialog, "Cancel", closeDialog);
  });
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
