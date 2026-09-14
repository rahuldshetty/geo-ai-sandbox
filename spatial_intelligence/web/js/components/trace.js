/* Agent trace: step grouping, full re-render, and incremental append. */

import { codeBlock, compactPreview, el, prettyValue } from "../dom.js";
import { state } from "../store.js";
import { cellOutputText, renderMarkdown, visibleTraceGroups } from "./markdown.js";
import { planFromTrace, renderPlan, updatePlan } from "./plan.js";
import { renderInteraction, reconcilePendingInteraction } from "./interaction.js";

function isCodeTool(name) {
  return name === "run_python";
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

function statusOf(item) {
  return (item && item.status) || "pending";
}

function toolPreview(name, call, result, isCode) {
  if (name === "write_plan" && call) {
    const items = parsePlanItems(call.args);
    const done = items.filter((item) => {
      const status = statusOf(item);
      return status === "completed" || status === "cancelled";
    }).length;
    const active = items.filter((item) => statusOf(item) === "in_progress").length;
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
  const details = el("details", { class: "trace-step tool-result" });
  const summary = el("summary", {});
  summary.append(
    el("span", { class: "trace-icon", text: "←" }),
    el("span", { class: "trace-name", text: step.name || "" }),
    el("span", { class: "trace-preview", text: compactPreview(step.content) })
  );
  details.append(summary);
  const body = el("div", { class: "trace-body" });
  body.append(codeBlock(prettyValue(step.content)));
  details.append(body);
  return details;
}

function traceTextNode(content) {
  const node = el("div", { class: "trace-step trace-text markdown" });
  node.dataset.source = content || "";
  node.innerHTML = renderMarkdown(node.dataset.source);
  return node;
}

export function groupTraceSteps(trace) {
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

  for (const step of trace || []) {
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
        for (const [key, candidate] of pending) {
          if ((candidate.call && candidate.call.name) === (step.name || "")) {
            group = candidate;
            pending.delete(key);
            break;
          }
        }
      }
      if (group) group.result = step;
      else groups.push({ type: "tool", call: null, result: step });
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

/** Render one grouped trace step; usage groups live in the status bar instead. */
export function renderStepNode(group) {
  if (!group) return null;
  if (group.type === "text") return traceTextNode(group.content);
  if (group.type === "tool") return toolStepNode(group);
  if (group.type === "plan") return renderPlan(group.items);
  return null;
}

export function renderTraceSteps(trace, cell = null) {
  const nodes = [];
  let interactionRendered = false;
  const renderedInteractionIds = new Set();
  const history = (cell && cell.interaction_history) || [];
  const plan = planFromTrace(trace || []);
  if (plan) nodes.push(renderPlan(plan));
  const groups = visibleTraceGroups(
    groupTraceSteps(trace || []),
    cell && cell.status,
    Boolean(cell && cellOutputText(cell).trim())
  );
  for (const group of groups) {
    if (group.type === "text") {
      nodes.push(traceTextNode(group.content));
    } else if (group.type === "tool") {
      const toolNode = toolStepNode(group);
      nodes.push(toolNode);
      const call = group.call;
      const completed =
        call && history.find((item) => item.tool_call_id === call.tool_call_id);
      const pending =
        cell &&
        cell.status === "waiting_for_input" &&
        cell.interaction &&
        call &&
        call.tool_call_id === cell.interaction.tool_call_id;
      if (completed || pending) {
        toolNode.classList.remove("pending");
        if (pending) toolNode.classList.add("waiting");
        const summary = toolNode.querySelector("summary");
        if (summary) {
          summary.append(
            el("span", {
              class: "trace-input-required",
              text: pending ? "input required" : "input provided",
            })
          );
        }
        nodes.push(renderInteraction(cell, completed || cell.interaction));
        if (completed) renderedInteractionIds.add(completed.id);
        else interactionRendered = true;
      }
    }
  }
  if (cell && cell.status === "waiting_for_input" && cell.interaction && !interactionRendered) {
    nodes.push(renderInteraction(cell));
  }
  for (const completed of history) {
    if (!renderedInteractionIds.has(completed.id)) {
      nodes.push(renderInteraction(cell, completed));
    }
  }
  return nodes;
}

/** Render a cell's own trace (plan node, steps, and pending interactions). */
export function renderTrace(cell) {
  return renderTraceSteps((cell && cell.trace) || [], cell);
}

function attachToolResult(node, result) {
  node.classList.remove("pending");
  const body = node.querySelector(".trace-body");
  if (body) {
    body.append(el("div", { class: "trace-io-label", text: "Output" }));
    body.append(codeBlock(prettyValue(result.content)));
  }
}

/**
 * Append one live trace step to an existing trace container.
 *
 * text_delta keeps growing the last text node, a tool_call registers a pending
 * node the matching tool_result fills in, and a plan step updates the plan node
 * in place.
 */
export function appendStep(container, step) {
  if (step.type === "text_delta") {
    const last = container.lastElementChild;
    if (last && last.classList.contains("trace-text")) {
      last.dataset.source = (last.dataset.source || "") + (step.content || "");
      last.innerHTML = renderMarkdown(last.dataset.source);
      container.scrollTop = container.scrollHeight;
      return;
    }
    container.append(traceTextNode(step.content));
  } else if (step.type === "text") {
    container.append(traceTextNode(step.content));
  } else if (step.type === "tool_call") {
    const node = toolStepNode({ call: step, result: null });
    container.append(node);
    const pending = container._pending || (container._pending = new Map());
    pending.set(step.tool_call_id || "seq:" + pending.size, { node, step });
  } else if (step.type === "plan") {
    updatePlan(container, step.items);
  } else if (step.type === "tool_result") {
    const pending = container._pending || (container._pending = new Map());
    let entry = null;
    if (step.tool_call_id && pending.has(step.tool_call_id)) {
      entry = pending.get(step.tool_call_id);
      pending.delete(step.tool_call_id);
    } else {
      for (const [key, candidate] of pending) {
        if ((candidate.step.name || "") === (step.name || "")) {
          entry = candidate;
          pending.delete(key);
          break;
        }
      }
    }
    if (entry) attachToolResult(entry.node, step);
    else container.append(toolResultNode(step));
  }
  container.scrollTop = container.scrollHeight;
}

/** Store a streamed trace step on its cell and append it to the live trace. */
export function applyTrace(data) {
  const idx = state.cells.findIndex((cell) => cell.id === data.id);
  if (idx === -1) return;
  if (!Array.isArray(state.cells[idx].trace)) state.cells[idx].trace = [];
  state.cells[idx].trace.push(data.step);
  if (data.step.type === "tool_call" && data.step.name === "request_user_input") {
    window.setTimeout(() => reconcilePendingInteraction(data.id), 150);
  }
  const container = document.querySelector('.cell[data-cell-id="' + data.id + '"] .trace');
  if (!container) return;
  appendStep(container, data.step);
}
