/* Workspace file tree: nested directory view used by the Data tab. */

import { el } from "../dom.js";

export function buildFileTree(files) {
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

export function renderFileTree(files) {
  const ul = el("ul", { class: "file-tree" });
  for (const node of sortedChildren(buildFileTree(files))) {
    ul.append(renderTreeNode(node));
  }
  return ul;
}

export function renderTreeNode(node) {
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
