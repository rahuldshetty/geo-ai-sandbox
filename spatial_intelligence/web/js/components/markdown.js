/* Markdown rendering and the text helpers every cell surface shares. */

export function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function renderMarkdown(source) {
  // `marked` ships as a classic script (index.html), so it is read from the
  // global scope at call time rather than bound at import time.
  const marked = globalThis.marked;
  if (!marked || typeof marked.parse !== "function") {
    throw new Error("The local marked renderer was not loaded");
  }

  // Marked intentionally does not sanitize raw HTML. Agent responses are
  // displayed in the application DOM, so escape HTML tokens while retaining
  // marked's full CommonMark/GFM support for tables, lists, code, and links.
  const renderer = new marked.Renderer();
  renderer.html = (token) => escapeHtml(typeof token === "string" ? token : token.text);
  return marked.parse(String(source), {
    gfm: true,
    renderer,
  });
}

export function cellOutputText(cell) {
  const parts = [];
  for (const output of (cell && cell.outputs) || []) {
    if (output.output_type === "stream") {
      if (output.text) parts.push(output.text);
    } else if (output.output_type === "error") {
      if (output.ename || output.evalue) {
        parts.push((output.ename ? output.ename + ": " : "") + (output.evalue || ""));
      }
      if (output.traceback && output.traceback.length) parts.push(output.traceback.join("\n"));
    }
  }
  return parts.join("\n");
}

export function visibleTraceGroups(groups, status, hasOutput) {
  let finalTextIndex = -1;
  if (status === "done" && hasOutput) {
    for (let index = groups.length - 1; index >= 0; index -= 1) {
      if (groups[index].type === "text") {
        finalTextIndex = index;
        break;
      }
    }
  }
  return groups.filter(
    (group, index) => group.type !== "usage" && index !== finalTextIndex
  );
}
