"use strict";
/* SolarWatch dashboard. No framework, no build step: plain DOM, hand-drawn SVG charts. */

const REFRESH_MS = 30000;
const PLATFORMS = {
  victron: "Victron VRM", sunsynk: "Sunsynk", goodwe_plus: "GoodWe SEMS+",
  sma: "SMA Sunny Portal", deye: "Deye Cloud", luxpower: "LuxPower", demo: "Demo",
};
const STATUS = {
  fault: { label: "Fault", rank: 0 }, offline: { label: "Offline", rank: 1 }, error: { label: "Read error", rank: 2 },
  warning: { label: "Warning", rank: 3 }, pending: { label: "Waiting", rank: 4 }, ok: { label: "Normal", rank: 5 },
};
const SERIES = [
  { key: "pv_w", label: "Solar", color: "var(--pv)" },
  { key: "load_w", label: "Home", color: "var(--load)" },
  { key: "battery_w", label: "Battery (+ charging)", color: "var(--batt)" },
  { key: "grid_w", label: "Grid (+ import)", color: "var(--grid)" },
];

const state = { data: null, platform: "all", attentionOnly: false, search: "", selected: null, range: 24, failed: false };
const $ = (s, el = document) => el.querySelector(s);

/* ---------- tiny DOM helper (textContent only: site names come from third-party APIs) ---------- */
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}
const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : String(kid));
  return el;
}

/* ---------- formatting ---------- */
const fmtW = (w) => {
  if (w == null || !isFinite(w)) return "–";
  const a = Math.abs(w);
  return a < 1000 ? `${Math.round(a)} W` : `${(a / 1000).toFixed(a < 10000 ? 1 : 0)} kW`;
};
const fmtWs = (w) => (w == null || !isFinite(w) ? "–" : (w <= -0.5 ? "−" : "") + fmtW(w));
const fmtKwh = (k) => (k == null || !isFinite(k) ? "–" : `${k < 100 ? k.toFixed(1) : Math.round(k)} kWh`);
const fmtPct = (p) => (p == null ? "–" : `${Math.round(p)}%`);
function ago(t) {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} days ago`;
}
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const ICON = {
  ok: "M3 8.5l3 3 7-7",
  warning: "M8 2l6.5 11.5h-13zM8 6.5v3.5M8 12v.1",
  fault: "M4 4l8 8M12 4l-8 8",
  error: "M4 4l8 8M12 4l-8 8",
  offline: "M2 8h12",
  pending: "M8 3v5l3 2",
};
const icon = (st) => s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" },
  s("path", { d: ICON[st] || ICON.pending, fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" }));
const statusPill = (st) => h("span", { class: `status ${st}` }, icon(st), STATUS[st]?.label || st);

/* ---------- data ---------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

async function refresh() {
  try {
    state.data = await api("/api/overview");
    state.failed = false;
    render();
    if (state.selected) loadDetail(state.selected, { quiet: true });
  } catch (e) {
    state.failed = true;
    renderSync();
  }
}

/* ---------- render: summary ---------- */
function renderSync() {
  const el = $("#sync");
  el.classList.toggle("bad", state.failed);
  if (state.failed) { el.textContent = "Can't reach the SolarWatch server. Retrying."; return; }
  const accs = state.data.accounts;
  const bad = accs.filter((a) => a.state === "error" || a.state === "auth_failed").length;
  const last = Math.max(0, ...accs.map((a) => a.last_success || 0));
  el.textContent = bad ? `${bad} account${bad > 1 ? "s" : ""} not updating · last data ${ago(last)}`
                       : `Live · polled ${ago(last)}`;
  el.classList.toggle("bad", bad > 0);
}

function renderSummary() {
  const { summary: sm } = state.data;
  const live = sm.by_status.ok + sm.by_status.warning;
  const attention = sm.by_status.fault + sm.by_status.error + sm.by_status.warning;
  const offline = sm.by_status.offline;
  const hl = $("#headline");
  if (!sm.sites) hl.textContent = "No sites yet. Add a platform account to config.yaml and they'll appear here.";
  else if (sm.pv_w >= 50) hl.textContent = `${fmtW(sm.pv_w)} of solar across ${live} live site${live === 1 ? "" : "s"} right now.`;
  else hl.textContent = `No sun on the panels. ${live} live site${live === 1 ? " is" : "s are"} running on battery and grid.`;
  const facts = [
    ["Sites", sm.sites],
    ["Need attention", attention, attention ? "attention" : null],
    ["Offline", offline, offline ? "offline" : null],
    ["Solar today", fmtKwh(sm.pv_kwh_today)],
    ["Home load now", fmtW(sm.load_w)],
    ["Average battery", fmtPct(sm.avg_soc)],
    ["Installed", sm.capacity_kwp ? `${sm.capacity_kwp.toFixed(1)} kWp` : "–"],
  ];
  $("#facts").replaceChildren(...facts.map(([k, v, c]) => h("div", { class: c }, h("dt", {}, k), h("dd", {}, v))));
}

/* ---------- render: site list ---------- */
function renderFilters() {
  const present = [...new Set(state.data.sites.map((x) => x.platform))].sort();
  const opts = [["all", "All"], ...present.map((p) => [p, PLATFORMS[p] || p])];
  if (state.platform !== "all" && !present.includes(state.platform)) state.platform = "all";
  $("#platformFilter").replaceChildren(...opts.map(([v, label]) => h("button", {
    type: "button", "aria-pressed": String(state.platform === v),
    onclick: () => { state.platform = v; render(); },
  }, label)));
}

function visibleSites() {
  const q = state.search.trim().toLowerCase();
  return state.data.sites.filter((x) =>
    (state.platform === "all" || x.platform === state.platform) &&
    (!state.attentionOnly || x.status !== "ok") &&
    (!q || x.name.toLowerCase().includes(q) || (PLATFORMS[x.platform] || "").toLowerCase().includes(q)));
}

function siteRow(x) {
  const soc = x.soc;
  const cls = soc == null ? "" : soc < state.data.thresholds.low_soc ? "low" : soc < 40 ? "mid" : "";
  const dim = x.status === "offline" || x.status === "pending";
  const batt = x.battery_w;
  const dir = batt == null || Math.abs(batt) < 20 ? "idle" : batt > 0 ? `charging ${fmtW(batt)}` : `using ${fmtW(batt)}`;
  const grid = x.grid_w;
  const gridTxt = grid == null ? "–" : Math.abs(grid) < 20 ? "0 W" : fmtW(grid);
  const gridSub = grid == null || Math.abs(grid) < 20 ? "idle" : grid > 0 ? "import" : "export";
  const select = () => selectSite(x.id);
  return h("li", {
    class: `site${dim ? " dim" : ""}`, tabindex: 0, "aria-current": String(state.selected === x.id),
    onclick: select, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); } },
  },
    h("div", { class: "who" },
      h("span", { class: "name" }, x.name),
      h("span", { class: "sub" }, statusPill(x.status), h("span", { class: "platform" }, PLATFORMS[x.platform] || x.platform))),
    h("div", { class: "batt", title: soc == null ? "No battery reading" : `Battery ${fmtPct(soc)}, ${dir}` },
      h("span", { class: `cell ${cls}` }, h("i", { style: `width:${Math.max(0, Math.min(100, soc ?? 0))}%` })),
      h("b", {}, fmtPct(soc), h("span", { class: "dir" }, dir.split(" ")[0]))),
    h("div", { class: "val" }, fmtW(x.pv_w), h("small", {}, fmtKwh(x.pv_kwh_today))),
    h("div", { class: "val c-load" }, fmtW(x.load_w), h("small", {}, "home")),
    h("div", { class: "val" }, gridTxt, h("small", {}, gridSub)),
    h("div", { class: "age" }, ago(x.reported_at || x.fetched_at)),
    h("div", { class: "mini" }, `Solar ${fmtW(x.pv_w)}`, `Home ${fmtW(x.load_w)}`, `Grid ${gridTxt} ${gridSub}`));
}

/* Which heading a site sits under. Offline also takes sites still waiting for their first data. */
const GROUPS = [
  { key: "attention", label: "Needs attention", statuses: ["fault", "error", "warning"] },
  { key: "normal", label: "Running normally", statuses: ["ok"] },
  { key: "offline", label: "Offline", statuses: ["offline", "pending"] },
];
const groupOf = (status) => (GROUPS.find((g) => g.statuses.includes(status)) || GROUPS[0]).key;

function renderSites() {
  const list = visibleSites();
  const byRank = (a, b) => (STATUS[a.status]?.rank ?? 9) - (STATUS[b.status]?.rank ?? 9) || a.name.localeCompare(b.name);
  const kids = [];
  for (const g of GROUPS) {
    const sites = list.filter((x) => groupOf(x.status) === g.key).sort(byRank);
    if (sites.length) {
      kids.push(h("li", { class: `group-label ${g.key}` }, `${g.label} (${sites.length})`), ...sites.map(siteRow));
    }
  }
  $("#sites").replaceChildren(...kids);
  const empty = $("#empty");
  empty.hidden = list.length > 0;
  empty.textContent = state.data.sites.length
    ? "No sites match these filters."
    : "Waiting for the first poll. Sites appear here once a platform account answers.";
}

function renderAccounts() {
  const accs = state.data.accounts;
  const bad = accs.filter((a) => a.state === "error" || a.state === "auth_failed");
  $("#accountsSummary").textContent = bad.length ? `${bad.length} with problems` : `${accs.length} connected`;
  if (bad.length) $("#accountsBox").open = true;
  const label = { ok: "OK", partial: "Some sites failed", error: "Retrying", auth_failed: "Sign-in refused", pending: "Starting", disabled: "Paused" };
  $("#accounts").replaceChildren(...accs.map((a) => h("li", {},
    h("span", {}, h("b", {}, a.id), ` · ${PLATFORMS[a.platform] || a.platform} · ${label[a.state] || a.state} · ${a.site_count} site${a.site_count === 1 ? "" : "s"}`),
    a.enabled ? h("button", { class: "btn", type: "button", onclick: (e) => pollNow(a.id, e.target) },
      a.state === "auth_failed" ? "Try sign-in again" : "Poll now") : h("span"),
    h("span", { class: "when" }, a.last_success ? `Last data ${ago(a.last_success)}` : "No data yet",
      a.next_poll ? ` · next poll ${clock(a.next_poll)}` : a.state === "auth_failed" ? " · paused until you retry" : ""),
    a.last_error ? h("span", { class: "err" }, a.state === "auth_failed"
      ? `${a.last_error}. Polling stopped so the account isn't locked; fix the password in the config, restart, or retry.`
      : a.last_error) : null)));
}

async function pollNow(id, btn) {
  btn.disabled = true;
  btn.textContent = "Polling…";
  try { await api(`/api/poll/${encodeURIComponent(id)}`, { method: "POST" }); } catch (_) { /* refresh shows state */ }
  setTimeout(refresh, 4000);
}

function render() {
  renderSync();
  renderSummary();
  renderFilters();
  renderSites();
  renderAccounts();
}

/* ---------- detail ---------- */
function selectSite(id) {
  state.selected = id;
  document.querySelectorAll(".site").forEach((el) => el.setAttribute("aria-current", "false"));
  renderSites();
  loadDetail(id);
}

async function loadDetail(id, { quiet = false } = {}) {
  const pane = $("#detail");
  try {
    const [site, hist, raw] = await Promise.all([
      api(`/api/sites/${id}`), api(`/api/sites/${id}/history?hours=${state.range}`), api(`/api/sites/${id}/raw`)]);
    if (state.selected !== id) return;
    drawDetail(pane, site, hist.points, raw);
    if (!quiet) { pane.classList.add("open"); pane.focus({ preventScroll: true }); }
  } catch (e) {
    if (!quiet) pane.replaceChildren(h("p", { class: "placeholder" }, "This site couldn't be loaded. It may have been removed from the config."));
  }
}

function drawDetail(pane, x, points, raw) {
  const frag = $("#detailTpl").content.cloneNode(true);
  $(".d-name", frag).textContent = x.name;
  const meta = [PLATFORMS[x.platform] || x.platform, x.capacity_kwp ? `${x.capacity_kwp} kWp` : null,
    `last report ${ago(x.reported_at || x.fetched_at)}`, `account ${x.account_id}`].filter(Boolean);
  $(".d-meta", frag).replaceChildren(statusPill(x.status), " ", meta.join(" · "));
  $(".d-alerts", frag).replaceChildren(...x.alerts.map((a) =>
    h("li", { class: a.level }, icon(a.level === "warning" ? "warning" : "fault"), a.message)));
  drawFlow($(".flow", frag), x);
  const today = [["Solar", x.pv_kwh_today, "--pv"], ["Home", x.load_kwh_today, "--load"],
    ["Imported", x.import_kwh_today, "--grid"], ["Exported", x.export_kwh_today, "--grid"]];
  $(".today", frag).replaceChildren(...today.map(([k, v, c]) =>
    h("div", { style: `--c: var(${c})` }, h("dt", {}, `${k} today`), h("dd", {}, fmtKwh(v)))));
  $(".legend", frag).replaceChildren(...SERIES.map((sr) => h("span", { style: `--c:${sr.color}` }, h("i"), sr.label)));
  frag.querySelectorAll(".range button").forEach((b) => {
    b.setAttribute("aria-pressed", String(Number(b.dataset.h) === state.range));
    b.addEventListener("click", () => { state.range = Number(b.dataset.h); loadDetail(x.id, { quiet: true }); });
  });
  $(".close", frag).addEventListener("click", () => { pane.classList.remove("open"); state.selected = null; renderSites(); });
  $(".raw pre", frag).textContent = raw && raw.raw ? JSON.stringify(raw.raw, null, 2) : "Nothing stored for this site yet.";
  const wasOpen = pane.querySelector(".raw")?.open;
  if (wasOpen) $(".raw", frag).open = true;
  pane.replaceChildren(frag);
  lineChart($(".chart.power", pane), points, SERIES, { fmt: fmtWs, zero: true, hours: state.range });
  lineChart($(".chart.soc", pane), points, [{ key: "soc", label: "Battery", color: "var(--batt)" }],
    { fmt: fmtPct, min: 0, max: 100, height: 110, hours: state.range });
}

/* Power flow: four sources/sinks around the inverter, dashes moving the way power moves. */
function drawFlow(svg, x) {
  const hub = [160, 100];
  const nodes = [
    { id: "pv", label: "Solar", at: [160, 26], w: x.pv_w, inward: true, color: "var(--pv)" },
    { id: "grid", label: x.grid_w < -20 ? "Grid (export)" : "Grid", at: [62, 100], w: x.grid_w, inward: (x.grid_w ?? 0) > 0, color: "var(--grid)" },
    { id: "home", label: "Home", at: [258, 100], w: x.load_w, inward: false, color: "var(--load)" },
    { id: "batt", label: x.battery_w > 20 ? "Battery (charging)" : x.battery_w < -20 ? "Battery (discharging)" : "Battery", at: [160, 174], w: x.battery_w, inward: (x.battery_w ?? 0) < 0, color: "var(--batt)",
      extra: x.soc != null ? fmtPct(x.soc) : null },
  ];
  const stale = x.status === "offline" || x.status === "pending";
  for (const n of nodes) {
    const live = !stale && n.w != null && Math.abs(n.w) >= 20;
    // path runs node -> hub; dashes move along it, reversed when power flows out of the hub
    svg.append(s("path", {
      class: `wire${live ? " live" : ""}${live && !n.inward ? " rev" : ""}`,
      d: `M${n.at[0]},${n.at[1]} L${hub[0]},${hub[1]}`, style: live ? `stroke:${n.color}` : null,
    }));
  }
  svg.append(s("circle", { class: "hub", cx: hub[0], cy: hub[1], r: 14 }));
  for (const n of nodes) {
    const [cx, cy] = n.at;
    svg.append(s("rect", { class: "node", x: cx - 60, y: cy - 20, width: 120, height: 40, rx: 6 }));
    svg.append(s("text", { x: cx, y: cy - 3 }, stale ? "–" : fmtW(n.w) + (n.extra ? ` · ${n.extra}` : "")));
    svg.append(s("text", { class: "lbl", x: cx, y: cy + 12 }, n.label));
  }
}

/* ---------- line chart (SVG, crosshair tooltip, gaps where polling stopped) ---------- */
function lineChart(box, points, series, opt) {
  const W = Math.max(280, box.clientWidth || 400), H = opt.height || 180;
  const m = { l: 46, r: 8, t: 8, b: 22 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
    "aria-label": `${series.map((x) => x.label).join(", ")} over the last ${opt.hours >= 48 ? opt.hours / 24 + " days" : opt.hours + " hours"}` });
  box.replaceChildren(svg);
  const pts = points.filter((p) => series.some((sr) => p[sr.key] != null));
  if (pts.length < 2) {
    svg.append(s("text", { class: "nodata", x: W / 2, y: H / 2 }, "Not enough history yet. Charts fill in as polls arrive."));
    return;
  }
  const t1 = Date.now() / 1000, t0 = t1 - opt.hours * 3600;
  let lo = opt.min ?? Infinity, hi = opt.max ?? -Infinity;
  if (opt.min == null || opt.max == null) {
    for (const p of pts) for (const sr of series) {
      const v = p[sr.key];
      if (v == null) continue;
      if (opt.min == null) lo = Math.min(lo, v);
      if (opt.max == null) hi = Math.max(hi, v);
    }
    if (opt.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  }
  const ticks = niceTicks(lo, hi, 4);
  lo = ticks[0]; hi = ticks[ticks.length - 1];
  const X = (t) => m.l + ((t - t0) / (t1 - t0)) * iw;
  const Y = (v) => m.t + ih - ((v - lo) / (hi - lo || 1)) * ih;

  for (const v of ticks) {
    svg.append(s("line", { class: v === 0 && opt.zero ? "zero" : "gridline", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    svg.append(s("text", { class: "tick", x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, opt.fmt(v).replace(".0 ", " ")));
  }
  const step = opt.hours <= 24 ? 6 * 3600 : 24 * 3600;
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
    const d = new Date(t * 1000);
    const label = opt.hours <= 24 ? clock(t) : d.toLocaleDateString([], { weekday: "short" });
    svg.append(s("text", { class: "tick", x: X(t), y: H - 4, "text-anchor": "middle" }, label));
  }
  // break lines across gaps longer than 3x the typical interval
  const gaps = pts.slice(1).map((p, i) => p.fetched_at - pts[i].fetched_at).sort((a, b) => a - b);
  const maxGap = Math.max(900, 3 * (gaps[Math.floor(gaps.length / 2)] || 300));
  for (const sr of series) {
    let d = "", prev = null;
    for (const p of pts) {
      const v = p[sr.key];
      if (v == null) { prev = null; continue; }
      const cmd = prev && p.fetched_at - prev <= maxGap ? "L" : "M";
      d += `${cmd}${X(p.fetched_at).toFixed(1)},${Y(v).toFixed(1)}`;
      prev = p.fetched_at;
    }
    svg.append(s("path", { class: "series", d, style: `stroke:${sr.color}` }));
  }
  // hover layer
  const cross = s("line", { class: "cross", y1: m.t, y2: m.t + ih, visibility: "hidden" });
  const dots = series.map((sr) => s("circle", { class: "dot", r: 4, style: `fill:${sr.color}`, visibility: "hidden" }));
  const hit = s("rect", { x: m.l, y: 0, width: iw, height: H, fill: "transparent" });
  svg.append(cross, ...dots, hit);
  const tip = $("#tip");
  const hide = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); };
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const t = t0 + (((e.clientX - r.left) * (W / r.width) - m.l) / iw) * (t1 - t0);
    let best = pts[0];
    for (const p of pts) if (Math.abs(p.fetched_at - t) < Math.abs(best.fetched_at - t)) best = p;
    const bx = X(best.fetched_at);
    cross.setAttribute("x1", bx); cross.setAttribute("x2", bx); cross.setAttribute("visibility", "visible");
    series.forEach((sr, i) => {
      const v = best[sr.key];
      dots[i].setAttribute("visibility", v == null ? "hidden" : "visible");
      if (v != null) { dots[i].setAttribute("cx", bx); dots[i].setAttribute("cy", Y(v)); }
    });
    const when = new Date(best.fetched_at * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
    tip.replaceChildren(h("b", {}, when), ...series.map((sr) =>
      h("div", { class: "row", style: `--c:${sr.color}` }, h("span", {}, sr.label.replace(/ \(.*/, "")), h("span", {}, opt.fmt(best[sr.key])))));
    tip.hidden = false;
    const tx = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = `${tx}px`;
    tip.style.top = `${Math.max(8, e.clientY - tip.offsetHeight - 12)}px`;
  });
}

function niceTicks(lo, hi, n) {
  if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
  if (lo === hi) { hi = lo + 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((k) => k >= raw);
  const out = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 0.001; v += step) out.push(+v.toFixed(6));
  if (out[out.length - 1] < hi) out.push(+(out[out.length - 1] + step).toFixed(6));
  return out;
}

/* ---------- wiring ---------- */
$("#attentionOnly").addEventListener("change", (e) => { state.attentionOnly = e.target.checked; renderSites(); });
$("#search").addEventListener("input", (e) => { state.search = e.target.value; renderSites(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#detail").classList.contains("open")) $(".close")?.click();
});
let resizeT;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => state.selected && loadDetail(state.selected, { quiet: true }), 200); });
refresh();
setInterval(refresh, REFRESH_MS);
