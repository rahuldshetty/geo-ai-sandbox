/* Data tab: refresh, local import, URL download, and the workspace file tree. */

import { getState, importFiles, importUrl as postImportUrl } from "../api.js";
import { applySnapshot, state } from "../store.js";
import { el, toast } from "../dom.js";
import { renderFileTree } from "../components/file-tree.js";

export function renderDataTab() {
  const wrap = el("div", {});

  if (!state.active_workspace) {
    wrap.append(
      el("div", { class: "empty-hint", text: "No workspace open — File → New or Open" })
    );
    return wrap;
  }

  const heading = el("div", { class: "data-heading" });
  heading.append(
    el("strong", { class: "data-heading-label", text: "Workspace files" }),
    el("button", {
      class: "data-refresh",
      text: "↻",
      title: "Refresh workspace files",
      "aria-label": "Refresh workspace files",
      onclick: () => refreshWorkspaceData(),
    })
  );
  wrap.append(heading);

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

export async function refreshWorkspaceData() {
  try {
    applySnapshot(await getState());
    toast("Workspace files refreshed");
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function importLocal(fileInput) {
  if (!fileInput.files.length) {
    toast("Choose a file or folder first");
    return;
  }
  try {
    const result = await importFiles(fileInput.files);
    const names = result.imported || [];
    fileInput.value = "";
    applySnapshot(await getState());
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
  try {
    await postImportUrl(url);
    urlInput.value = "";
    applySnapshot(await getState());
    toast("Downloaded into the workspace data folder");
  } catch (e) {
    toast(e.message || String(e));
  }
}

export function renderDataOnly() {
  const content = document.getElementById("tab-content");
  if (content && state.selected_tab === "Data") {
    content.replaceChildren(renderDataTab());
  }
}
