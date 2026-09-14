/* Settings dialog: model, theme, transient attempts, agent-step recording. */

"use strict";

import { updateSettings } from "../api.js";
import { closeDialog, dialogActions, el, openDialog, toast } from "../dom.js";
import { setState, state } from "../store.js";

export function openSettingsDialog() {
  openDialog((dialog) => {
    dialog.append(el("h3", { text: "Settings" }));

    const row = (label, hint, input) => {
      const wrapper = el("div", { class: "settings-row" });
      const labels = el("div", { class: "settings-labels" });
      labels.append(el("label", { text: label }));
      if (hint) labels.append(el("span", { class: "settings-hint", text: hint }));
      wrapper.append(labels, input);
      return wrapper;
    };

    const modelInput = el("input", {
      type: "text",
      value: state.settings.model || "",
      placeholder: "openai:gpt-4o",
    });

    const themeSelect = el("select", {});
    themeSelect.append(
      el("option", { value: "light", text: "Light" }),
      el("option", { value: "dark", text: "Dark" })
    );
    themeSelect.value = state.settings.theme === "dark" ? "dark" : "light";

    const retriesInput = el("input", {
      type: "number",
      min: "1",
      step: "1",
      value: String(state.settings.max_retries != null ? state.settings.max_retries : 5),
    });

    const recordStepsInput = el("input", { type: "checkbox" });
    recordStepsInput.checked = state.settings.record_agent_steps !== false;

    dialog.append(
      row("Model", "e.g. openai:gpt-4o, openai-chat:qwen3 (local endpoint), anthropic:claude-sonnet-4-5", modelInput),
      row("Theme", "app shell appearance", themeSelect),
      row(
        "Transient attempts",
        "maximum provider attempts before a workspace or map change",
        retriesInput
      ),
      row(
        "Record agent steps",
        "append generated tool calls, outputs, and responses to the notebook",
        recordStepsInput
      )
    );

    dialogActions(dialog, "Save", async () => {
      try {
        const settings = await updateSettings({
          model: modelInput.value.trim(),
          theme: themeSelect.value,
          max_retries: Number(retriesInput.value) || 5,
          record_agent_steps: recordStepsInput.checked,
        });
        setState({ settings });
        closeDialog();
        toast("Settings saved");
      } catch (error) {
        toast(error.message || String(error));
      }
    });
  });
}
