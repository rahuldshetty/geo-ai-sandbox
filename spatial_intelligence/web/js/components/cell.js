/* One cell: header actions, source, output, trace, and its job strip. */

import { el, expandableContent, toast, usageLabel, usageTitle } from "../dom.js";
import { deleteCell, moveCell, runCell, stopCell, updateCell } from "../api.js";
import { state } from "../store.js";
import { cellOutputText, renderMarkdown } from "./markdown.js";
import { renderTraceSteps } from "./trace.js";
import { renderJob } from "./progress.js";
import { editMarkdown } from "../pages/cells.js";

/** Fire a cell action, surfacing its failure the way the old app did. */
function act(action) {
  Promise.resolve()
    .then(action)
    .catch((error) => toast(error.message || String(error)));
}

function cellBadge(kind, geoai) {
  if (kind === "tool") return "agent tool";
  if (geoai.kind === "response") return "agent response";
  if (geoai.kind === "interaction_response") return "user choice";
  if (kind === "markdown") return "md";
  if (kind === "prompt") return "prompt";
  return "py";
}

function cellStatusText(status) {
  if (status === "waiting_for_input") return "waiting";
  if (status === "running") return "running";
  if (status === "error") return "failed";
  if (status === "done") return "done";
  return "";
}

function renderHeader(cell, kind, geoai, generated) {
  const header = el("div", { class: "cell-header" });
  if (kind !== "markdown" && kind !== "tool" && cell.status !== "waiting_for_input") {
    if (cell.status === "running" && kind === "prompt") {
      header.append(
        el("button", {
          class: "run-btn stop",
          text: "■",
          title: "Stop",
          onclick: () => act(() => stopCell(cell.id)),
        })
      );
    } else {
      header.append(
        el("button", {
          class: "run-btn",
          text: "▶",
          title: "Run",
          onclick: () => act(() => runCell(cell.id)),
        })
      );
    }
  }
  header.append(
    el("span", {
      class: "counter",
      text: "In[" + (cell.execution_count != null ? cell.execution_count : " ") + "]",
    }),
    el("span", { class: "badge", text: cellBadge(kind, geoai) })
  );
  if (kind === "tool") {
    header.append(
      el("span", { class: "agent-action-name", text: geoai.tool_name || "tool" }),
      el("span", {
        class: "agent-action-status status-" + (cell.status || "idle"),
        text: cellStatusText(cell.status),
      })
    );
  }
  header.append(el("span", { class: "spacer" }));

  if (kind === "markdown") {
    if (!generated) {
      header.append(
        el("button", {
          class: "icon",
          text: "edit",
          title: "Edit",
          onclick: () => editMarkdown(cell),
        }),
        el("button", {
          class: "icon",
          text: "✕",
          title: "Delete",
          onclick: () => act(() => deleteCell(cell.id)),
        })
      );
    }
  } else if (kind === "tool") {
    header.append(
      el("button", {
        class: "icon",
        text: "✕",
        title: "Delete",
        onclick: () => deleteCell(cell.id),
      })
    );
  } else {
    header.append(
      el("button", {
        class: "icon",
        text: "↑",
        title: "Move up",
        onclick: () => act(() => moveCell(cell.id, -1)),
      }),
      el("button", {
        class: "icon",
        text: "↓",
        title: "Move down",
        onclick: () => act(() => moveCell(cell.id, 1)),
      }),
      el("button", {
        class: "icon",
        text: "✕",
        title: "Delete",
        onclick: () => deleteCell(cell.id),
      })
    );
  }
  return header;
}

function renderBody(cell, kind, geoai, source, generated) {
  const body = el("div", { class: "cell-body" });
  if (kind === "markdown") {
    const markdown = el("div", { class: "markdown" });
    markdown.innerHTML = renderMarkdown(source);
    body.append(markdown);
  } else if (kind === "tool") {
    body.append(
      expandableContent("Input", geoai.args === undefined ? source : JSON.stringify(geoai.args, null, 2))
    );
  } else {
    const ta = el("textarea", {
      rows: Math.min(12, Math.max(2, source.split("\n").length)),
    });
    ta.value = source;
    if (generated) {
      ta.readOnly = true;
    } else {
      ta.addEventListener("input", () => {
        const idx = state.cells.findIndex((c) => c.id === cell.id);
        if (idx >= 0) state.cells[idx].source = ta.value;
      });
      ta.addEventListener("blur", () => {
        act(() => updateCell(cell.id, ta.value));
      });
    }
    body.append(ta);
  }
  return body;
}

function renderOutputRow(cell, kind) {
  const row = el("div", { class: "cell-out" });
  row.append(
    el("span", {
      class: "counter",
      text: "Out[" + (cell.execution_count != null ? cell.execution_count : " ") + "]",
    })
  );
  if (cell.status === "running") {
    row.append(el("span", { class: "running", text: "running…" }));
  } else if (cell.status === "waiting_for_input") {
    row.append(el("span", { class: "running waiting", text: "waiting for input" }));
  } else if (cell.status === "stopped") {
    row.append(el("span", { class: "running stopped", text: "stopped" }));
  }
  if (kind === "prompt" && cell.usage) {
    row.append(
      el("span", { class: "usage", text: usageLabel(cell.usage), title: usageTitle(cell.usage) })
    );
  }
  return row;
}

function renderOutputBlock(cell) {
  const block = el("div", { class: "cell-out-block" });
  const text = cellOutputText(cell);
  if (text) {
    if (cell.status === "error") {
      block.append(el("pre", { class: "error", text }));
    } else {
      const markdown = el("div", { class: "markdown" });
      markdown.innerHTML = renderMarkdown(text);
      block.append(markdown);
    }
  }
  return block;
}

function renderToolOutput(cell) {
  const text = cellOutputText(cell);
  if (!text) return null;
  const output = el("div", { class: "agent-action-output" });
  output.append(
    expandableContent(
      cell.status === "error" ? "Error" : "Output",
      text,
      cell.status === "error" ? "error" : ""
    )
  );
  return output;
}

/**
 * Build one cell.
 *
 * `jobs` is jobsFor(cell.id): progress jobs owned by this cell. They render in
 * the cell's job strip whatever the cell kind, so a job parented to a python or
 * tool cell is visible instead of being dropped.
 */
export function renderCell(cell, jobs = []) {
  const kind = cell.kind;
  const geoai = (cell.metadata && cell.metadata.geoai) || {};
  const source = cell.source == null ? "" : String(cell.source);
  const generated = Boolean(geoai.generated);
  const box = el("div", {
    class: "cell" + (generated ? " generated-cell" : ""),
    "data-cell-id": cell.id,
  });

  box.append(renderHeader(cell, kind, geoai, generated));
  box.append(renderBody(cell, kind, geoai, source, generated));

  if (kind !== "markdown" && kind !== "tool") {
    box.append(renderOutputRow(cell, kind));
  } else if (kind === "tool") {
    const output = renderToolOutput(cell);
    if (output) box.append(output);
  }

  const jobNodes = jobs.map((job) => renderJob(job, "trace"));
  const traceNodes = renderTraceSteps(cell.trace || [], cell).filter(Boolean);
  // A prompt cell always mounts its trace container, even while it is empty:
  // live steps stream in before the first one is part of the cell snapshot, and
  // appending needs the mount point to already exist. Other kinds mount one
  // only when they have something to show (a job strip, or a replayed trace).
  if (kind === "prompt" || traceNodes.length || jobNodes.length) {
    const trace = el("div", { class: "trace" });
    trace.append(...traceNodes, ...jobNodes);
    box.append(trace);
  }

  if (kind !== "markdown" && kind !== "tool") {
    box.append(renderOutputBlock(cell));
  }

  return box;
}
