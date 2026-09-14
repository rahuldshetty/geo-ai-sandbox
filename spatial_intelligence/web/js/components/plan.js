/* Agent plan: the checklist a write_plan tool call maintains. */

import { el } from "../dom.js";

export function planFromTrace(steps) {
  let items = null;
  for (const step of steps || []) {
    if (step.type === "plan") items = step.items || [];
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

export function renderPlan(items) {
  const list = items || [];
  const total = list.length;
  const done = list.filter((item) => {
    const status = statusOf(item);
    return status === "completed" || status === "cancelled";
  }).length;
  const details = el("details", { class: "trace-step trace-plan", open: true });
  const summary = el("summary", {});
  summary.append(
    el("span", { class: "trace-icon", text: "☑" }),
    el("span", { class: "trace-name", text: "Plan" }),
    el("span", { class: "trace-preview", text: done + "/" + total + " done" })
  );
  details.append(summary);
  const body = el("div", { class: "trace-body" });
  const bar = el("div", { class: "plan-progress" });
  const fill = el("div", { class: "plan-progress-fill" });
  fill.style.width = (total ? Math.round((done / total) * 100) : 0) + "%";
  bar.append(fill);
  body.append(bar, planList(list));
  details.append(body);
  return details;
}

export function updatePlan(container, items) {
  const existing = container.querySelector(".trace-plan");
  if (!items || !items.length) {
    if (existing) existing.remove();
    return;
  }
  const node = renderPlan(items);
  if (existing) existing.replaceWith(node);
  else container.prepend(node);
}
