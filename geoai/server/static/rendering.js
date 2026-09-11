(function (root, factory) {
  "use strict";

  const api = factory(root.marked);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.GeoAIRendering = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (markedApi) {
  "use strict";

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderMarkdown(source) {
    if (!markedApi || typeof markedApi.parse !== "function") {
      throw new Error("The local marked renderer was not loaded");
    }

    // Marked intentionally does not sanitize raw HTML. Agent responses are
    // displayed in the application DOM, so escape HTML tokens while retaining
    // marked's full CommonMark/GFM support for tables, lists, code, and links.
    const renderer = new markedApi.Renderer();
    renderer.html = ({ text }) => escapeHtml(text);
    return markedApi.parse(String(source), {
      gfm: true,
      renderer,
    });
  }

  function visibleTraceGroups(groups, status, hasOutput) {
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

  return { escapeHtml, renderMarkdown, visibleTraceGroups };
});
