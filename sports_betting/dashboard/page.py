"""The dashboard's single self-contained page.

No external script, font or stylesheet: it is served from loopback and must render with no
network. It reads `/api/status` and builds every node with `textContent`, never `innerHTML`,
so a league or job name from an artifact cannot inject markup. Colours are the dataviz
reference palette's roles (status: good/warning/serious/critical; series 1 blue), each status
shipped with an icon and a label so colour never carries meaning alone.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sports betting &middot; collection dashboard</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f9; --surface-1: #ffffff;
  --text-primary: #191919; --text-secondary: #666666;
  --grid: #e7e7e7; --baseline: #c2c2c2; --border: rgba(11,11,11,0.10);
  --series-1: #257de0; --track: #efefef;
  --good: #0aac23; --warning: #fbb315; --serious: #f18a59; --critical: #dc3c3d;
  --neutral: #9c9c9c;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0b0b0b; --surface-1: #191919;
    --text-primary: #efefef; --text-secondary: #9c9c9c;
    --grid: #303030; --baseline: #474747; --border: rgba(255,255,255,0.10);
    --series-1: #338ced; --track: #383838; --neutral: #666666;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 1180px; margin: 0 auto; padding: 24px 20px 48px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px 20px; }
h1 { font-size: 20px; margin: 0; font-weight: 650; }
h2 { font-size: 15px; margin: 0 0 4px; font-weight: 650; }
.sub { color: var(--text-secondary); font-size: 12.5px; }
.num { font-variant-numeric: tabular-nums; }
.card {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 18px;
}
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 12px; margin: 18px 0; }
.tile .value { font-size: 28px; font-weight: 650; margin: 2px 0; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 12px; margin-top: 12px; }
section.card { margin-top: 12px; }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
  margin-bottom: 10px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-weight: 550; color: var(--text-secondary); font-size: 12px;
  padding: 6px 8px; border-bottom: 1px solid var(--baseline); white-space: nowrap; }
td { padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.r, th.r { text-align: right; }
.reason { color: var(--text-secondary); font-size: 12.5px; max-width: 420px;
  overflow-wrap: anywhere; }
.pill { display: inline-flex; align-items: center; gap: 6px; font-weight: 550;
  white-space: nowrap; }
.dot { width: 18px; height: 18px; border-radius: 50%; display: inline-grid;
  place-items: center; font-size: 11px; line-height: 1; color: #fff; }
.s-good { background: var(--good); }
.s-warning { background: var(--warning); color: #0b0b0b; }
.s-serious { background: var(--serious); }
.s-critical { background: var(--critical); }
.s-neutral { background: var(--neutral); }
.verdict { font-size: 14px; }
.meter { position: relative; height: 10px; border-radius: 5px; background: var(--track);
  overflow: hidden; margin: 6px 0 2px; }
.meter > span { position: absolute; inset: 0 auto 0 0; border-radius: 5px;
  background: var(--series-1); }
.meter > span.warn { background: var(--warning); }
.meter > span.crit { background: var(--critical); }
.bars { display: grid; grid-template-columns: max-content 1fr max-content;
  gap: 6px 10px; align-items: center; }
.bars .track { height: 14px; position: relative; }
.bars .bar { position: absolute; inset: 0 auto 0 0; background: var(--series-1);
  border-radius: 0 4px 4px 0; min-width: 2px; }
.bars .bar:hover { filter: brightness(1.12); }
.empty { color: var(--text-secondary); padding: 6px 0; }
.err { color: var(--critical); font-size: 12.5px; }
code { font-size: 12px; color: var(--text-secondary); }
@media (forced-colors: active) { .dot, .meter > span, .bars .bar { forced-color-adjust: none; } }
</style>
</head>
<body>
<main>
  <header>
    <h1>Sports betting &middot; collection</h1>
    <span id="verdict" class="verdict pill"></span>
    <span class="sub" id="generated">loading&hellip;</span>
  </header>

  <div class="tiles" id="tiles"></div>

  <section class="card">
    <div class="head"><h2>Collection jobs</h2><code id="health-path"></code></div>
    <div id="jobs"></div>
  </section>

  <div class="grid2">
    <section class="card">
      <div class="head"><h2>Upcoming fair lines by sport</h2>
        <span class="sub">what the overlay can price now</span></div>
      <div id="lines"></div>
    </section>
    <section class="card">
      <div class="head"><h2>Overlay coverage by sport</h2>
        <span class="sub">Mise-o-jeu+ events browsed, all days</span></div>
      <div id="coverage"></div>
    </section>
  </div>

  <section class="card">
    <div class="head"><h2>Archive datasets</h2><code id="catalog-path"></code></div>
    <div id="datasets"></div>
  </section>
</main>
<script>
"use strict";
const REFRESH_MS = 60000;
const STATUS = {
  ok: ["good", "\\u2713", "ok"],
  idle: ["warning", "!", "idle"],
  degraded: ["warning", "!", "degraded"],
  stale: ["serious", "\\u25C6", "stale"],
  failing: ["critical", "\\u2715", "failing"],
  "never-run": ["neutral", "\\u25CB", "never run"],
  skipped: ["neutral", "\\u2013", "skipped"],
};

function el(tag, props, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "text") node.textContent = value;
    else if (key === "style") Object.assign(node.style, value);
    else node.setAttribute(key, value);
  }
  for (const child of children || []) if (child) node.append(child);
  return node;
}

function pill(status) {
  const [tone, icon, label] = STATUS[status] || ["neutral", "?", status];
  return el("span", { class: "pill" }, [
    el("span", { class: "dot s-" + tone, "aria-hidden": "true", text: icon }),
    el("span", { text: label }),
  ]);
}

function age(stamp, now) {
  if (!stamp) return "never";
  const seconds = Math.round((now - new Date(stamp)) / 1000);
  if (Number.isNaN(seconds)) return "unknown";
  const ahead = seconds < 0, s = Math.abs(seconds);
  const text = s >= 86400 ? Math.floor(s / 86400) + "d" : s >= 3600 ? Math.floor(s / 3600) + "h"
    : s >= 60 ? Math.floor(s / 60) + "m" : s + "s";
  return ahead ? "in " + text : text + " ago";
}

const fmt = new Intl.NumberFormat();
// "2026-11-20 14:05 UTC" from an ISO stamp; the raw value stays in the cell's tooltip.
const stamp = iso => iso ? iso.replace("T", " ").slice(0, 16) + " UTC" : "\\u2013";
const pct = (part, whole) => whole ? Math.round(100 * part / whole) + "%" : "\\u2013";

function table(headers, rows) {
  return el("table", {}, [
    el("thead", {}, [el("tr", {}, headers.map(([label, cls]) =>
      el("th", cls ? { class: cls } : {}, [label])))]),
    el("tbody", {}, rows.map(cells => el("tr", {}, cells.map(([value, cls, title]) => {
      const props = cls ? { class: cls } : {};
      if (title) props.title = title;
      return el("td", props, [value instanceof Node ? value : String(value)]);
    })))),
  ]);
}

// `budget` meters warn as spend nears the limit; a share meter (coverage) never does.
function meter(used, limit, budget) {
  const share = limit ? used / limit : 0;
  const cls = !budget ? "" : share >= 1 ? "crit" : share >= 0.75 ? "warn" : "";
  return el("div", { class: "meter", role: "meter", "aria-valuemin": "0",
    "aria-valuemax": String(limit), "aria-valuenow": String(used),
    title: fmt.format(used) + " of " + fmt.format(limit) + " (" + pct(used, limit) + ")" },
    [el("span", { class: cls, style: { width: Math.min(100, share * 100) + "%" } })]);
}

function tile(label, value, detail, extra) {
  return el("div", { class: "card tile" }, [
    el("div", { class: "sub", text: label }),
    el("div", { class: "value num", text: value }),
    extra,
    el("div", { class: "sub", text: detail }),
  ]);
}

function renderTiles(data, now) {
  const lines = data.fair_lines, cov = data.coverage;
  const tiles = [tile("Upcoming fair lines", fmt.format(lines.total),
    lines.total ? "prices as of " + age(lines.as_of, now) + " \\u00b7 " + lines.sharp + " Pinnacle"
      : "none yet \\u2014 run collect --provider the-odds-api")];
  for (const row of data.quotas.rows) {
    tiles.push(tile(row.label, fmt.format(row.used) + " / " + fmt.format(row.limit),
      row.period + " \\u00b7 " + fmt.format(Math.max(0, row.limit - row.used)) + " left",
      meter(row.used, row.limit, true)));
  }
  tiles.push(tile("Overlay priced", pct(cov.priced, cov.events),
    fmt.format(cov.priced) + " of " + fmt.format(cov.events) + " browsed events"));
  document.getElementById("tiles").replaceChildren(...tiles);
}

function renderJobs(health, now) {
  document.getElementById("health-path").textContent = health.artifact;
  const verdict = document.getElementById("verdict");
  if (!health.readable) {
    verdict.replaceChildren(pill("never-run"));
    document.getElementById("jobs").replaceChildren(el("div", { class: "empty", text: health.error }));
    return;
  }
  verdict.replaceChildren(health.ok ? pill("ok") : pill("failing"),
    el("span", { class: "sub", text: health.ok ? "all jobs healthy"
      : health.problems.length + " job(s) need attention: " + health.problems.join(", ") }));
  const rows = health.jobs.map(job => [
    [job.job], [pill(job.status)],
    [age(job.last_success, now), "num", job.last_success || ""],
    [age(job.last_wrote, now), "num", job.last_wrote || ""],
    [fmt.format(job.last_added) + " / " + fmt.format(job.last_fetched), "r num"],
    [fmt.format(job.runs), "r num"],
    [fmt.format(job.failures), "r num"],
    [el("div", { class: "reason", text: job.status === "ok" ? "" : (job.reason || "") })],
  ]);
  document.getElementById("jobs").replaceChildren(rows.length ? table(
    [["Job"], ["Status"], ["Last ok"], ["Last wrote"], ["Added / fetched", "r"], ["Runs", "r"],
     ["Failures", "r"], ["Reason"]], rows) : el("div", { class: "empty", text: "no jobs recorded" }));
}

function renderBars(target, items, emptyText) {
  const box = document.getElementById(target);
  if (!items.length) { box.replaceChildren(el("div", { class: "empty", text: emptyText })); return; }
  const top = Math.max(...items.map(item => item.value));
  const cells = [];
  for (const item of items) {
    cells.push(el("span", { text: item.label }),
      el("div", { class: "track" }, [el("div", { class: "bar", title: item.title,
        style: { width: (100 * item.value / top) + "%" } })]),
      el("span", { class: "num sub", text: item.text }));
  }
  box.replaceChildren(el("div", { class: "bars" }, cells));
}

function renderCoverage(cov) {
  const box = document.getElementById("coverage");
  if (!cov.events) {
    box.replaceChildren(el("div", { class: "empty",
      text: "nothing browsed yet \\u2014 open Mise-o-jeu+ with the extension loaded" }));
    return;
  }
  box.replaceChildren(table([["Sport"], ["Events", "r"], ["Priced", "r"], ["Share priced"]],
    cov.by_sport.map(row => [[row.sport], [fmt.format(row.events), "r num"],
      [fmt.format(row.priced), "r num"], [meter(row.priced, row.events)]])));
}

function renderDatasets(sets, now) {
  document.getElementById("catalog-path").textContent = sets.catalog;
  const box = document.getElementById("datasets");
  const parts = [];
  if (!sets.rows.length) parts.push(el("div", { class: "empty",
    text: sets.present ? "the catalog lists no datasets" : "no catalog at this ARCHIVE_ROOT" }));
  else parts.push(table([["Dataset"], ["Rows", "r"], ["Partitions", "r"], ["Newest record"],
    ["Catalog updated"]], sets.rows.map(row => [[row.dataset], [fmt.format(row.rows), "r num"],
      [fmt.format(row.partitions), "r num"], [stamp(row.newest), "num", row.newest || ""],
      [age(row.updated_at, now), "num", row.updated_at || ""]])));
  if (sets.unreadable.length) parts.push(el("div", { class: "err",
    text: "unreadable manifests: " + sets.unreadable.join(", ") }));
  box.replaceChildren(...parts);
}

function render(data) {
  const now = new Date(data.generated_at);
  document.getElementById("generated").textContent =
    "updated " + now.toLocaleTimeString() + " \\u00b7 refreshes every minute";
  renderTiles(data, now);
  renderJobs(data.health, now);
  renderBars("lines", data.fair_lines.by_sport.map(row => ({ label: row.sport, value: row.lines,
    text: fmt.format(row.lines), title: row.sport + ": " + fmt.format(row.lines) + " lines" })),
    "no upcoming fair lines in the archive");
  renderCoverage(data.coverage);
  renderDatasets(data.datasets, now);
  const quotaError = data.quotas.error;
  if (quotaError) document.getElementById("tiles").append(
    el("div", { class: "err", text: "quota ledger " + data.quotas.artifact + ": " + quotaError }));
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    render(await response.json());
  } catch (error) {
    document.getElementById("generated").textContent = "could not load status: " + error.message;
  }
}
refresh();
setInterval(refresh, REFRESH_MS);
</script>
</body>
</html>
"""
