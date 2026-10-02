// Shows the local service's verdicts in a floating panel. Verdicts live in this tab's
// memory only: nothing is written to extension storage, and closing the tab drops them.
(() => {
  const verdicts = new Map();
  const pageEvents = new Set();
  const pagePriced = new Set();
  const state = { status: "waiting for odds on this page…", linesAsOf: null, minEdge: null };
  let root = null;
  let list = null;
  let statusLine = null;
  let collapsed = false;

  const percent = (value) => `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
  const time = (iso) =>
    new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const STYLE = `
    :host { all: initial; }
    .panel { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; width: 340px;
      max-height: 60vh; display: flex; flex-direction: column; background: #fff; color: #1b1b1f;
      border: 1px solid #c9c9d1; border-radius: 10px; box-shadow: 0 6px 24px rgba(0,0,0,.18);
      font: 13px/1.35 system-ui, sans-serif; }
    .head { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid #e4e4ea; }
    .title { font-weight: 600; flex: 1; }
    button { font: inherit; border: 1px solid #c9c9d1; background: #f6f6f8; border-radius: 6px; cursor: pointer; padding: 2px 8px; }
    .status { padding: 6px 10px; color: #55555f; font-size: 12px; }
    .list { overflow: auto; padding: 0 10px 8px; }
    .event { padding: 6px 0; border-top: 1px solid #eeeef2; }
    .name { font-weight: 600; }
    .when { color: #6b6b75; font-size: 12px; }
    .side { display: grid; grid-template-columns: 1fr auto auto auto; gap: 8px; font-variant-numeric: tabular-nums; }
    .value { color: #0a7a3d; font-weight: 700; }
    .novalue { color: #6b6b75; }
    .muted { color: #8a8a94; font-size: 12px; padding-top: 6px; }
    .collapsed .list, .collapsed .status { display: none; }
  `;

  function mount() {
    if (root || !document.body) return;
    const host = element("div");
    host.id = "mise-overlay-root";
    const shadow = host.attachShadow({ mode: "closed" });
    const style = element("style");
    style.textContent = STYLE;
    root = element("div", "panel");
    const head = element("div", "head");
    head.append(element("span", "title", "Value overlay"));
    const toggle = element("button", "", "–");
    toggle.addEventListener("click", () => {
      collapsed = !collapsed;
      root.classList.toggle("collapsed", collapsed);
      toggle.textContent = collapsed ? "+" : "–";
    });
    head.append(toggle);
    statusLine = element("div", "status");
    list = element("div", "list");
    root.append(head, statusLine, list);
    shadow.append(style, root);
    document.body.append(host);
    render();
  }

  function render() {
    if (!root) return;
    const parts = [state.status];
    if (pageEvents.size) parts.push(`priced ${pagePriced.size} of ${pageEvents.size} events seen`);
    if (state.minEdge !== null) parts.push(`min edge ${percent(state.minEdge)}`);
    if (state.linesAsOf) parts.push(`fair lines as of ${time(state.linesAsOf)}`);
    statusLine.textContent = parts.join(" · ");

    list.replaceChildren();
    const all = [...verdicts.values()];
    const matched = all
      .filter((event) => event.status === "matched")
      .sort((a, b) => {
        const best = (event) => Math.max(...event.sides.map((side) => side.edge));
        return best(b) - best(a);
      });
    for (const event of matched) {
      const row = element("div", "event");
      row.append(element("div", "name", event.name + (event.boosted ? " (boosted)" : "")));
      row.append(element("div", "when", `${time(event.start)} · ${event.books} book(s)`));
      for (const side of event.sides) {
        const line = element("div", "side");
        line.append(element("span", "", side.team));
        line.append(element("span", "", `${side.price.toFixed(2)}`));
        line.append(element("span", "novalue", `min ${side.min_price.toFixed(2)}`));
        line.append(element("span", side.value ? "value" : "novalue", percent(side.edge)));
        row.append(line);
      }
      list.append(row);
    }
    const unpriced = pageEvents.size - pagePriced.size;
    if (unpriced) list.append(element("div", "muted", `${unpriced} event(s) with no fair line`));
    if (!all.length) list.append(element("div", "muted", "Open a competition page to see verdicts."));
  }

  function apply(response) {
    if (!response || !response.ok) {
      state.status = `local service unavailable — run: sports-betting overlay-serve`;
      render();
      return;
    }
    state.status = `${response.lines} fair line(s) loaded`;
    state.linesAsOf = response.lines_as_of;
    state.minEdge = response.min_edge;
    for (const event of response.events || []) verdicts.set(event.event_id, event);
    for (const id of response.page?.event_ids || []) pageEvents.add(id);
    for (const id of response.page?.priced_ids || []) pagePriced.add(id);
    render();
  }

  window.addEventListener("message", (message) => {
    if (message.source !== window || message.data?.source !== "mise-overlay-capture") return;
    chrome.runtime.sendMessage({ type: "evaluate", body: message.data.body }, apply);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
  chrome.runtime.sendMessage({ type: "health" }, (response) => {
    if (!response || !response.ok) apply(response);
    else {
      state.linesAsOf = response.lines_as_of;
      state.minEdge = response.min_edge;
      state.status = `${response.lines} fair line(s) loaded`;
      render();
    }
  });
})();
