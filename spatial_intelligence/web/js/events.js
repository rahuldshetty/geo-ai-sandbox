/* Server-sent events: one EventSource for the whole app, with reconnect
backoff (the browser's built-in retry is short and bursty after a restart). */

"use strict";

const EVENT_ROUTES = ["cell", "trace", "job", "map", "files", "settings"];
const INITIAL_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;

export function connectEvents(handlers = {}) {
  const routes = EVENT_ROUTES.map((name) => {
    const handler = handlers["on" + name.charAt(0).toUpperCase() + name.slice(1)];
    return [name, typeof handler === "function" ? handler : null];
  });

  let source = null;
  let retryTimer = null;
  let delay = INITIAL_DELAY_MS;
  let closed = false;

  const open = () => {
    if (closed) return;
    source = new EventSource("/api/events");
    source.addEventListener("open", () => {
      delay = INITIAL_DELAY_MS;
    });
    for (const [name, handler] of routes) {
      if (!handler) continue;
      source.addEventListener(name, (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch (_) {
          return;
        }
        handler(payload);
      });
    }
    source.onerror = () => {
      // The browser would reconnect immediately and forever; closing the
      // stream hands retry timing to the exponential backoff below.
      if (source) {
        source.onerror = null;
        source.close();
        source = null;
      }
      if (closed || retryTimer !== null) return;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        open();
      }, delay);
      delay = Math.min(delay * 2, MAX_DELAY_MS);
    };
  };

  open();

  return () => {
    closed = true;
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (source) {
      source.onerror = null;
      source.close();
      source = null;
    }
  };
}
