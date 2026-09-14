/* Deferred tool input: the form a request_user_input tool call waits on. */

import { el } from "../dom.js";
import { cancelInteraction, getState, respondInteraction } from "../api.js";
import { normalizeCells, setState } from "../store.js";
import { renderCellsOnly } from "../pages/cells.js";

export function renderInteraction(cell, interaction = cell.interaction) {
  const request = interaction;
  const submitted = request.submitted === true;
  const answers = request.answers || {};
  const form = el(submitted ? "div" : "form", {
    class: "interaction-form" + (submitted ? " submitted" : ""),
  });
  form.append(
    el("h4", { text: request.title || "Input required" }),
    el("p", { class: "interaction-prompt", text: request.prompt || "" })
  );
  const controls = new Map();
  for (const field of request.fields || []) {
    const group = el("fieldset", { class: "interaction-field" });
    group.append(el("legend", { text: field.label + (field.required === false ? "" : " *") }));
    if (field.description) {
      group.append(el("p", { class: "interaction-description", text: field.description }));
    }
    if (field.type === "text") {
      const input = el("input", {
        type: "text",
        placeholder: field.placeholder || "",
        value:
          submitted && answers[field.id] != null
            ? String(answers[field.id])
            : field.default == null
              ? ""
              : String(field.default),
      });
      input.disabled = submitted;
      controls.set(field.id, { field, nodes: [input] });
      group.append(input);
    } else if (field.type === "confirmation") {
      const input = el("input", { type: "checkbox" });
      input.checked = submitted ? Boolean(answers[field.id]) : Boolean(field.default);
      input.disabled = submitted;
      const option = el("label", { class: "interaction-option compact" });
      option.append(input, el("span", { text: field.description || "Yes" }));
      controls.set(field.id, { field, nodes: [input] });
      group.append(option);
    } else {
      const nodes = [];
      for (const option of field.options || []) {
        const input = el("input", {
          type: field.type === "radio" ? "radio" : "checkbox",
          name: "interaction-" + request.id + "-" + field.id,
          value: option.value,
        });
        const selected = submitted
          ? Array.isArray(answers[field.id])
            ? answers[field.id].includes(option.value)
            : answers[field.id] === option.value
          : Array.isArray(field.default)
            ? field.default.includes(option.value)
            : field.default === option.value || (!field.default && option.recommended);
        input.checked = selected;
        input.disabled = submitted;
        const card = el("label", {
          class: "interaction-option" + (option.thumbnail_url ? " with-thumbnail" : ""),
        });
        if (option.thumbnail_url) {
          card.append(el("img", { src: option.thumbnail_url, alt: "" }));
        }
        const copy = el("span", { class: "interaction-option-copy" });
        copy.append(
          el("strong", { text: option.label + (option.recommended ? " — Recommended" : "") })
        );
        if (option.description) copy.append(el("small", { text: option.description }));
        card.append(input, copy);
        group.append(card);
        nodes.push(input);
      }
      controls.set(field.id, { field, nodes });
    }
    form.append(group);
  }
  if (submitted) {
    form.append(el("div", { class: "interaction-submitted", text: "Selection submitted" }));
    return form;
  }
  const error = el("div", { class: "interaction-error" });
  const submit = el("button", {
    type: "submit",
    class: "primary",
    text: request.submit_label || "Continue",
  });
  const actions = el("div", { class: "interaction-actions" });
  if (request.allow_cancel !== false) {
    actions.append(
      el("button", {
        type: "button",
        text: "Cancel",
        onclick: async () => {
          try {
            await cancelInteraction(cell.id, request.id);
            cell.status = "stopped";
            cell.interaction = null;
            renderCellsOnly();
          } catch (e) {
            error.textContent = e.message || String(e);
          }
        },
      })
    );
  }
  actions.append(submit);
  form.append(error, actions);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = {};
    for (const [id, control] of controls) {
      const { field, nodes } = control;
      if (field.type === "text") values[id] = nodes[0].value.trim();
      else if (field.type === "confirmation") values[id] = nodes[0].checked;
      else if (field.type === "radio") values[id] = (nodes.find((n) => n.checked) || {}).value;
      else values[id] = nodes.filter((n) => n.checked).map((n) => n.value);
      const empty =
        values[id] == null ||
        values[id] === "" ||
        (Array.isArray(values[id]) && !values[id].length);
      if (field.required !== false && empty) {
        error.textContent = "Please complete " + field.label + ".";
        return;
      }
    }
    submit.disabled = true;
    try {
      await respondInteraction(cell.id, request.id, values);
      const completed = { ...request, answers: values, submitted: true };
      const history = cell.interaction_history || [];
      if (!history.some((item) => item.id === completed.id)) {
        cell.interaction_history = [...history, completed];
      }
      cell.status = "running";
      cell.interaction = null;
      renderCellsOnly();
    } catch (e) {
      submit.disabled = false;
      error.textContent = e.message || String(e);
    }
  });
  return form;
}

export function revealInteraction(cellId) {
  window.requestAnimationFrame(() => {
    const form = document.querySelector(
      '.cell[data-cell-id="' + cellId + '"] .interaction-form'
    );
    if (form) form.scrollIntoView({ behavior: "smooth", block: "center" });
  });
}

export async function reconcilePendingInteraction(cellId, attempt = 0) {
  try {
    const snap = await getState();
    const serverCell = (snap.cells || []).find((cell) => cell.id === cellId);
    if (serverCell) {
      setState({ cells: normalizeCells(snap.cells || []) });
      renderCellsOnly();
      if (serverCell.status === "waiting_for_input" && serverCell.interaction) {
        revealInteraction(cellId);
        return;
      }
      if (serverCell.status !== "running") return;
    }
  } catch (_) {
    // SSE remains the primary update path; retry briefly while the deferred
    // tool transitions from a streamed call into a waiting prompt.
  }
  if (attempt < 20) {
    window.setTimeout(() => reconcilePendingInteraction(cellId, attempt + 1), 250);
  }
}
