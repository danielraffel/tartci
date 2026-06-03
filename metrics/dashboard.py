#!/usr/bin/env python3
"""CI-metrics dashboard generator (stdlib only).

Reads a JSONL metrics store (one CI run per line) and writes a single
self-contained ``index.html`` that opens directly via ``file://`` — no
server, no fetch(), no build step, no third-party libraries. The metrics
data is embedded as a JSON ``const`` inside the page and all charts are
hand-drawn with inline SVG, so the page works fully offline.

Usage:
    python3 dashboard.py [metrics.jsonl] [index.html]

When run, it also prints a short text report to stdout so it is useful
headless / in a terminal.
"""

import json
import os
import sys
import html
import statistics as st
from collections import defaultdict, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT = os.path.join(HERE, "metrics.jsonl")
DEFAULT_OUTPUT = os.path.join(HERE, "index.html")

# Trend threshold: latest build_s within +/- this fraction of the rolling
# median counts as "flat" (–). Above => slower (worse, ▲). Below => faster.
TREND_THRESHOLD = 0.20


def load_rows(path):
    """Read JSONL; skip blank lines and tolerate malformed rows."""
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                sys.stderr.write(
                    "warning: skipping malformed line %d in %s: %s\n"
                    % (lineno, path, exc)
                )
                continue
            if isinstance(obj, dict):
                # stamp original order so the UI can show a stable run index
                obj.setdefault("_idx", len(rows))
                rows.append(obj)
    return rows


def num(row, key):
    """Return a float for ``key`` if present and numeric, else None."""
    v = row.get(key)
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def fmt(v, suffix=""):
    if v is None:
        return "-"
    if isinstance(v, float):
        if v == int(v):
            return "%d%s" % (int(v), suffix)
        return "%.1f%s" % (v, suffix)
    return "%s%s" % (v, suffix)


def median_of(rows, key):
    vals = [num(r, key) for r in rows]
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def trend_for(rows):
    """Return (symbol, label) comparing latest build_s to rolling median."""
    vals = [num(r, "build_s") for r in rows]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return ("–", "n/a")
    latest = vals[-1]
    med = st.median(vals)
    if med == 0:
        return ("–", "flat")
    delta = (latest - med) / med
    if delta > TREND_THRESHOLD:
        return ("▲", "slower (%.0f%% vs median)" % (delta * 100))
    if delta < -TREND_THRESHOLD:
        return ("▼", "faster (%.0f%% vs median)" % (delta * 100))
    return ("–", "flat (%.0f%% vs median)" % (delta * 100))


def group_by_os_mode(rows):
    by = defaultdict(list)
    for r in rows:
        by[(r.get("os", "?"), r.get("mode", "?"))].append(r)
    return OrderedDict(sorted(by.items()))


def text_report(rows):
    """Build the stdout text report (mirrors report.py's shape, extended)."""
    lines = []
    by = group_by_os_mode(rows)
    header = "%-18s %4s %12s %14s %11s %10s %6s" % (
        "os/mode", "runs", "last build_s", "median build_s",
        "last cache%", "median c%", "trend",
    )
    lines.append(header)
    lines.append("-" * len(header))
    for key, rs in by.items():
        last = rs[-1]
        med_build = median_of(rs, "build_s")
        med_cache = median_of(rs, "ccache_hit_pct")
        sym, _ = trend_for(rs)
        lines.append(
            "%-18s %4d %12s %14s %11s %10s %6s" % (
                "%s/%s" % key,
                len(rs),
                fmt(num(last, "build_s")),
                fmt(med_build),
                fmt(num(last, "ccache_hit_pct")),
                fmt(med_cache),
                sym,
            )
        )
    tp = sum(int(num(r, "tests_passed") or 0) for r in rows)
    tt = sum(int(num(r, "tests_total") or 0) for r in rows)
    lines.append("")
    lines.append("total runs: %d   tests passed/total (where reported): %d/%d"
                 % (len(rows), tp, tt))
    return "\n".join(lines)


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CI Metrics Dashboard</title>
<style>
  :root {
    --bg: #0f1216; --panel: #171b21; --panel2: #1d222a; --line: #2a313b;
    --fg: #e6e9ee; --muted: #9aa4b2; --accent: #4ea1ff; --good: #4cd07a;
    --bad: #ff6b6b; --flat: #9aa4b2;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 24px 20px 64px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 28px 0 10px; color: var(--fg); }
  .note {
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 12px 14px; color: var(--muted); font-size: 13px; margin: 12px 0 8px;
  }
  .note code { color: var(--accent); }
  table { width: 100%; border-collapse: collapse; background: var(--panel);
    border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--line);
    white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  th { background: var(--panel2); color: var(--muted); font-weight: 600;
    font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
  tr:last-child td { border-bottom: none; }
  .up { color: var(--bad); } .down { color: var(--good); } .flat { color: var(--flat); }
  .controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
    margin: 6px 0 12px; }
  .controls button, .controls select {
    background: var(--panel2); color: var(--fg); border: 1px solid var(--line);
    border-radius: 6px; padding: 5px 10px; font-size: 13px; cursor: pointer; }
  .controls button.active { border-color: var(--accent); color: var(--accent); }
  .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px,1fr));
    gap: 14px; }
  .chart { background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 10px 12px; }
  .chart h3 { margin: 0 0 6px; font-size: 13px; color: var(--muted); font-weight: 600; }
  .layout { display: grid; grid-template-columns: 340px 1fr; gap: 14px; }
  @media (max-width: 760px) { .layout { grid-template-columns: 1fr; } }
  .runlist { background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; overflow: hidden; max-height: 520px; overflow-y: auto; }
  .runrow { padding: 9px 12px; border-bottom: 1px solid var(--line); cursor: pointer;
    display: flex; justify-content: space-between; gap: 8px; }
  .runrow:hover { background: var(--panel2); }
  .runrow.sel { background: var(--panel2); border-left: 3px solid var(--accent); }
  .runrow small { color: var(--muted); }
  .badge { font-size: 11px; padding: 1px 7px; border-radius: 10px;
    border: 1px solid var(--line); color: var(--muted); }
  .detail { background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 14px; min-height: 120px; }
  .detail h3 { margin: 0 0 10px; }
  .kv { display: grid; grid-template-columns: 160px 1fr; gap: 4px 12px; }
  .kv div.k { color: var(--muted); }
  .kv div.v { word-break: break-word; }
  .subtable { margin-top: 10px; }
  svg { display: block; width: 100%; height: auto; }
  .axis { stroke: var(--line); stroke-width: 1; }
  .gridline { stroke: var(--line); stroke-width: 1; stroke-dasharray: 2 3; opacity: .6; }
  .axlabel { fill: var(--muted); font-size: 10px; }
  text.tick { fill: var(--muted); font-size: 10px; }
  .empty { color: var(--muted); padding: 12px; }
  .legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px;
    color: var(--muted); margin: 6px 0 0; }
  .legend span b { display: inline-block; width: 10px; height: 10px;
    border-radius: 2px; margin-right: 4px; vertical-align: middle; }

  /* --- Title + info icon --- */
  .titlebar { display: flex; align-items: center; gap: 8px; margin: 0 0 4px; }
  .titlebar h1 { margin: 0; }
  /* Last-run hero */
  .lastrun { background: linear-gradient(180deg, var(--panel2), var(--panel));
    border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px;
    margin: 10px 0 22px; cursor: pointer; }
  .lastrun:hover { border-color: var(--accent); }
  .lr-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
  .lr-title { font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
    color: var(--muted); font-weight: 700; }
  .lr-os { font-weight: 700; font-size: 16px; }
  .lr-ts { color: var(--muted); font-size: 13px; }
  .lr-note { color: var(--muted); font-size: 12px; font-style: italic; }
  .lr-trend { margin-left: auto; font-size: 18px; }
  .lr-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
  .lr-stats .stat { background: var(--bg); border: 1px solid var(--line);
    border-radius: 9px; padding: 12px 14px; }
  .lr-stats .stat .v { font-size: 24px; font-weight: 700; line-height: 1.1; }
  .lr-stats .stat .l { font-size: 11px; color: var(--muted); text-transform: uppercase;
    letter-spacing: .05em; margin-top: 4px; }
  .lr-stats .stat.primary { border-color: var(--accent); }
  .lr-stats .stat.primary .v { color: var(--accent); }
  .info {
    position: relative; display: inline-flex; align-items: center;
    justify-content: center; width: 20px; height: 20px; border-radius: 50%;
    border: 1px solid var(--line); background: var(--panel2); color: var(--muted);
    font-size: 12px; font-style: normal; cursor: help; user-select: none;
  }
  .info:hover, .info.open { color: var(--accent); border-color: var(--accent); }
  .info .info-pop {
    position: absolute; top: 26px; left: 0; z-index: 20; width: 360px;
    max-width: 78vw; background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 12px 14px; color: var(--muted); font-size: 13px;
    line-height: 1.5; box-shadow: 0 8px 28px rgba(0,0,0,.5);
    opacity: 0; visibility: hidden; transform: translateY(-4px);
    transition: opacity .12s ease, transform .12s ease, visibility .12s; text-align: left; }
  .info:hover .info-pop, .info.open .info-pop {
    opacity: 1; visibility: visible; transform: translateY(0); }
  .info .info-pop code { color: var(--accent); }

  /* --- Cold/warm mode badges --- */
  .mode-badge { font-size: 11px; padding: 1px 8px; border-radius: 10px;
    border: 1px solid var(--line); text-transform: uppercase; letter-spacing: .03em; }
  .mode-cold { color: #8fc7ff; border-color: #2f5d86; background: rgba(78,161,255,.12); }
  .mode-warm { color: #ffce8f; border-color: #7a5a2f; background: rgba(255,180,84,.14); }
  .os-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
    margin-right: 6px; vertical-align: middle; }

  /* --- Chart hover tooltip --- */
  .chart { position: relative; }
  .chart-tip {
    position: absolute; pointer-events: none; z-index: 30; display: none;
    background: #0b0e12; border: 1px solid var(--accent); border-radius: 7px;
    padding: 7px 10px; font-size: 12px; line-height: 1.45; color: var(--fg);
    box-shadow: 0 6px 20px rgba(0,0,0,.6); white-space: nowrap; max-width: 280px; }
  .chart-tip .tip-os { font-weight: 700; }
  .chart-tip .tip-val { color: var(--accent); font-weight: 700; }
  .chart-tip .tip-meta { color: var(--muted); }
  svg circle.dot { cursor: pointer; }
  svg circle.hit { fill: transparent; }
  .pt-label { fill: var(--fg); font-size: 10px; font-weight: 600;
    paint-order: stroke; stroke: #0b0e12; stroke-width: 3px; stroke-linejoin: round; }
</style>
</head>
<body>
<div class="wrap">
  <div class="titlebar">
    <h1>CI Metrics Dashboard</h1>
    <i class="info" id="infoIcon" tabindex="0" role="button" aria-label="About this dashboard">&#9432;
      <span class="info-pop">
        Self-contained, offline dashboard regenerated by
        <code>python3 dashboard.py</code>, which reads
        <code>metrics.jsonl</code> (one JSON object per CI run) and embeds the
        data directly in this page. No server, network, or build step is
        required &mdash; just open this file. Re-run the script after new runs
        land to refresh.
      </span>
    </i>
  </div>

  <div id="lastrun"></div>

  <h2>Recent runs &mdash; most recent first</h2>
  <div id="recent"></div>

  <h2>Summary &mdash; latest run per os/mode</h2>
  <div id="summary"></div>

  <h2>Trends &mdash; full history per OS</h2>
  <div class="controls" id="metricControls"></div>
  <div class="charts" id="charts"></div>

  <h2>Sessions &mdash; click a run for its full readout</h2>
  <div class="layout">
    <div class="runlist" id="runlist"></div>
    <div class="detail" id="detail"><div class="empty">Select a run on the left to see its full record.</div></div>
  </div>
</div>

<script>
const DATA = __DATA__;
const TREND_THRESHOLD = __THRESHOLD__;
const METRICS = [
  {key: "build_s", label: "build_s (lower=better)", unit: "s"},
  {key: "ccache_hit_pct", label: "ccache_hit_pct (higher=better)", unit: "%"},
  {key: "ctest_s", label: "ctest_s (lower=better)", unit: "s"},
];
// Stable color per OS.
const OS_COLORS = {linux: "#4ea1ff", macos: "#4cd07a", windows: "#ffb454", "?": "#c08cff"};
function osColor(os){ return OS_COLORS[os] || "#c08cff"; }

function num(row, k){
  const v = row[k];
  if (typeof v === "number" && isFinite(v)) return v;
  return null;
}
function fmt(v, suffix){
  if (v === null || v === undefined) return "-";
  if (typeof v === "number") {
    const s = (Math.round(v*10)/10);
    return (Number.isInteger(s) ? s : s.toFixed(1)) + (suffix||"");
  }
  return String(v) + (suffix||"");
}
function median(arr){
  const a = arr.filter(v => v !== null && v !== undefined).sort((x,y)=>x-y);
  if (!a.length) return null;
  const m = Math.floor(a.length/2);
  return a.length % 2 ? a[m] : (a[m-1]+a[m])/2;
}
function esc(s){
  return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
function osDot(os){ return `<span class="os-dot" style="background:${osColor(os)}"></span>`; }
function modeBadge(mode){
  const m = (mode||"?").toLowerCase();
  const cls = m === "cold" ? "mode-cold" : (m === "warm" ? "mode-warm" : "");
  return `<span class="mode-badge ${cls}">${esc(mode||"?")}</span>`;
}

// ---- Info icon (hover via CSS; click toggles for touch/keyboard) ----
function wireInfoIcon(){
  const ic = document.getElementById("infoIcon");
  if (!ic) return;
  ic.addEventListener("click", e => { e.stopPropagation(); ic.classList.toggle("open"); });
  ic.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ic.classList.toggle("open"); }
    if (e.key === "Escape") ic.classList.remove("open");
  });
  document.addEventListener("click", () => ic.classList.remove("open"));
}

// ---- Summary table ----
function groupByOsMode(rows){
  const m = new Map();
  for (const r of rows){
    const k = (r.os||"?") + "/" + (r.mode||"?");
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(r);
  }
  return new Map([...m.entries()].sort((a,b)=>a[0]<b[0]?-1:1));
}
function trendFor(rows){
  const vals = rows.map(r=>num(r,"build_s")).filter(v=>v!==null);
  if (vals.length < 2) return {sym:"–", cls:"flat", label:"n/a"};
  const latest = vals[vals.length-1], med = median(vals);
  if (!med) return {sym:"–", cls:"flat", label:"flat"};
  const d = (latest - med)/med;
  if (d > TREND_THRESHOLD) return {sym:"▲", cls:"up", label:`slower (${Math.round(d*100)}% vs median)`};
  if (d < -TREND_THRESHOLD) return {sym:"▼", cls:"down", label:`faster (${Math.round(d*100)}% vs median)`};
  return {sym:"–", cls:"flat", label:`flat (${Math.round(d*100)}% vs median)`};
}
function renderSummary(){
  const by = groupByOsMode(DATA);
  let h = `<table><thead><tr>
    <th>os / mode</th><th>runs</th><th>last build_s</th><th>median build_s</th>
    <th>last configure_s</th><th>last ctest_s</th><th>last cache%</th>
    <th>median cache%</th><th>tests</th><th title="latest build_s vs rolling median, +/-${Math.round(TREND_THRESHOLD*100)}%">trend</th>
    </tr></thead><tbody>`;
  for (const [k, rs] of by){
    const last = rs[rs.length-1];
    const t = trendFor(rs);
    const tp = num(last,"tests_passed"), tt = num(last,"tests_total");
    const tests = (tt!==null) ? `${tp!==null?tp:"?"}/${tt}` : "-";
    h += `<tr>
      <td>${esc(k)}</td>
      <td>${rs.length}</td>
      <td>${fmt(num(last,"build_s"))}</td>
      <td>${fmt(median(rs.map(r=>num(r,"build_s"))))}</td>
      <td>${fmt(num(last,"configure_s"))}</td>
      <td>${fmt(num(last,"ctest_s"))}</td>
      <td>${fmt(num(last,"ccache_hit_pct"))}</td>
      <td>${fmt(median(rs.map(r=>num(r,"ccache_hit_pct"))))}</td>
      <td>${tests}</td>
      <td class="${t.cls}" title="${esc(t.label)}">${t.sym}</td>
    </tr>`;
  }
  h += `</tbody></table>`;
  document.getElementById("summary").innerHTML = DATA.length ? h
    : `<div class="empty">No runs in metrics.jsonl yet.</div>`;
}

// ---- Recent runs table (headline view, most-recent-first) ----
const RECENT_N = 12;
// Per-run trend: compare this run's build_s to the rolling median of its
// own os/mode group (same threshold + semantics as the summary trend).
function runTrend(run){
  const groupKey = (run.os||"?") + "/" + (run.mode||"?");
  const vals = DATA
    .filter(r => ((r.os||"?")+"/"+(r.mode||"?")) === groupKey)
    .map(r => num(r,"build_s")).filter(v => v !== null);
  const b = num(run,"build_s");
  if (b === null || vals.length < 2) return {sym:"–", cls:"flat", label:"n/a"};
  const med = median(vals);
  if (!med) return {sym:"–", cls:"flat", label:"flat"};
  const d = (b - med)/med;
  if (d > TREND_THRESHOLD) return {sym:"▲", cls:"up", label:`slower (${Math.round(d*100)}% vs ${groupKey} median)`};
  if (d < -TREND_THRESHOLD) return {sym:"▼", cls:"down", label:`faster (${Math.round(d*100)}% vs ${groupKey} median)`};
  return {sym:"–", cls:"flat", label:`flat (${Math.round(d*100)}% vs ${groupKey} median)`};
}
function renderLastRun(){
  const el = document.getElementById("lastrun");
  if (!el) return;
  if (!DATA.length){ el.innerHTML = ""; return; }
  const r = [...DATA].sort((a,b)=>(b._idx||0)-(a._idx||0))[0];
  const t = runTrend(r);
  const tp = num(r,"tests_passed"), tt = num(r,"tests_total");
  const tests = (tt!==null) ? `${tp!==null?tp:"?"}/${tt}` : "-";
  const stat = (label, val, primary) =>
    `<div class="stat ${primary?'primary':''}"><div class="v">${val}</div><div class="l">${label}</div></div>`;
  let tiles = stat("build_s", fmt(num(r,"build_s"),"s"), true)
            + stat("configure_s", fmt(num(r,"configure_s"),"s"))
            + stat("ctest_s", fmt(num(r,"ctest_s"),"s"))
            + stat("ccache hit", fmt(num(r,"ccache_hit_pct"),"%"))
            + stat("tests", tests);
  if (num(r,"vm_clone_s")!==null) tiles += stat("vm clone_s", fmt(num(r,"vm_clone_s"),"s"));
  el.innerHTML = `<div class="lastrun" data-idx="${r._idx}">
    <div class="lr-head">
      <span class="lr-title">Last run</span>
      ${osDot(r.os)}<span class="lr-os" style="color:${osColor(r.os)}">${esc(r.os||"?")}</span>
      ${modeBadge(r.mode)}
      <span class="lr-ts">${esc(r.ts||"?")}${r.arch?(" &middot; "+esc(r.arch)):""}${r.provider?(" &middot; "+esc(r.provider)):""}</span>
      ${r.note?`<span class="lr-note">${esc(r.note)}</span>`:""}
      <span class="lr-trend ${t.cls}" title="${esc(t.label)}">${t.sym}</span>
    </div>
    <div class="lr-stats">${tiles}</div>
  </div>`;
  const card = el.querySelector(".lastrun");
  if (card) card.onclick = () => {
    selectRun(r._idx);
    const det = document.getElementById("detail");
    if (det && det.scrollIntoView) det.scrollIntoView({behavior:"smooth", block:"center"});
  };
}

function renderRecent(){
  const el = document.getElementById("recent");
  if (!DATA.length){ el.innerHTML = `<div class="empty">No runs in metrics.jsonl yet.</div>`; return; }
  const ordered = [...DATA].sort((a,b)=>(b._idx||0)-(a._idx||0));
  const rows = ordered.slice(0, RECENT_N);
  let h = `<table><thead><tr>
    <th>date / ts</th><th>os</th><th>mode</th>
    <th>build_s</th><th>configure_s</th><th>ctest_s</th><th>cache%</th>
    <th>tests</th><th title="this run's build_s vs its os/mode rolling median, +/-${Math.round(TREND_THRESHOLD*100)}%">trend</th>
    </tr></thead><tbody>`;
  for (const r of rows){
    const t = runTrend(r);
    const tp = num(r,"tests_passed"), tt = num(r,"tests_total");
    const tests = (tt!==null) ? `${tp!==null?tp:"?"}/${tt}` : "-";
    h += `<tr data-idx="${r._idx}" style="cursor:pointer">
      <td>${esc(r.ts||"?")}</td>
      <td>${osDot(r.os)}${esc(r.os||"?")}</td>
      <td>${modeBadge(r.mode)}</td>
      <td>${fmt(num(r,"build_s"))}</td>
      <td>${fmt(num(r,"configure_s"))}</td>
      <td>${fmt(num(r,"ctest_s"))}</td>
      <td>${fmt(num(r,"ccache_hit_pct"))}</td>
      <td>${tests}</td>
      <td class="${t.cls}" title="${esc(t.label)}">${t.sym}</td>
    </tr>`;
  }
  h += `</tbody></table>`;
  if (DATA.length > RECENT_N)
    h += `<div class="legend">Showing ${rows.length} of ${DATA.length} runs.</div>`;
  el.innerHTML = h;
  // click-through to the session detail readout
  el.querySelectorAll("tr[data-idx]").forEach(tr => {
    tr.onclick = () => {
      const idx = Number(tr.dataset.idx);
      selectRun(idx);
      const det = document.getElementById("detail");
      if (det && det.scrollIntoView) det.scrollIntoView({behavior:"smooth", block:"center"});
    };
  });
}

// ---- SVG line chart (one metric, lines per OS) ----
function lineChart(metricKey, unit){
  const W = 460, H = 220, PADL = 44, PADR = 12, PADT = 12, PADB = 28;
  const plotW = W - PADL - PADR, plotH = H - PADT - PADB;
  // group points by OS, x = per-OS run index, y = metric value
  const byOs = new Map();
  for (const r of DATA){
    const y = num(r, metricKey);
    if (y === null) continue;
    const os = r.os || "?";
    if (!byOs.has(os)) byOs.set(os, []);
    byOs.get(os).push({r, y});
  }
  const allY = [];
  for (const pts of byOs.values()) for (const p of pts) allY.push(p.y);
  if (!allY.length){
    return `<div class="empty">No ${esc(metricKey)} data.</div>`;
  }
  let maxX = 0;
  for (const pts of byOs.values()) maxX = Math.max(maxX, pts.length - 1);
  maxX = Math.max(maxX, 1);
  let minY = Math.min(...allY), maxY = Math.max(...allY);
  if (minY === maxY){ minY = Math.min(0, minY); maxY = maxY + 1; }
  // pad y range a touch
  const pad = (maxY - minY) * 0.08;
  minY = Math.max(0, minY - pad); maxY = maxY + pad;
  const sx = x => PADL + (x / maxX) * plotW;
  const sy = y => PADT + plotH - ((y - minY) / (maxY - minY)) * plotH;

  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${esc(metricKey)} over runs">`;
  // y gridlines + ticks (4 steps)
  for (let i=0;i<=4;i++){
    const yv = minY + (maxY-minY)*i/4;
    const yp = sy(yv);
    svg += `<line class="gridline" x1="${PADL}" y1="${yp.toFixed(1)}" x2="${W-PADR}" y2="${yp.toFixed(1)}"/>`;
    svg += `<text class="tick" x="${PADL-6}" y="${(yp+3).toFixed(1)}" text-anchor="end">${fmt(yv)}</text>`;
  }
  // axes
  svg += `<line class="axis" x1="${PADL}" y1="${PADT}" x2="${PADL}" y2="${PADT+plotH}"/>`;
  svg += `<line class="axis" x1="${PADL}" y1="${PADT+plotH}" x2="${W-PADR}" y2="${PADT+plotH}"/>`;
  // x ticks (run index)
  const xticks = Math.min(maxX, 6);
  for (let i=0;i<=xticks;i++){
    const xv = Math.round(maxX * i / xticks);
    const xp = sx(xv);
    svg += `<text class="tick" x="${xp.toFixed(1)}" y="${(PADT+plotH+14).toFixed(1)}" text-anchor="middle">${xv}</text>`;
  }
  svg += `<text class="axlabel" x="${(PADL+plotW/2).toFixed(1)}" y="${H-2}" text-anchor="middle">run index (per OS)</text>`;
  svg += `<text class="axlabel" transform="translate(11,${(PADT+plotH/2).toFixed(1)}) rotate(-90)" text-anchor="middle">${esc(unit||"")}</text>`;
  // lines + points per OS
  for (const [os, pts] of byOs){
    const col = osColor(os);
    let d = "";
    pts.forEach((p, i) => {
      const X = sx(i), Y = sy(p.y);
      d += (i===0 ? "M" : "L") + X.toFixed(1) + " " + Y.toFixed(1) + " ";
    });
    if (pts.length > 1)
      svg += `<path d="${d}" fill="none" stroke="${col}" stroke-width="2"/>`;
    pts.forEach((p, i) => {
      const X = sx(i), Y = sy(p.y);
      // data attributes drive the JS hover tooltip (works from file://)
      const da = `data-os="${esc(os)}" data-val="${esc(fmt(p.y, " "+(unit||"")).trim())}"`
        + ` data-metric="${esc(metricKey)}" data-mode="${esc(p.r.mode||"?")}"`
        + ` data-ts="${esc(p.r.ts||"")}" data-col="${col}"`;
      // visible dot
      svg += `<circle class="dot" cx="${X.toFixed(1)}" cy="${Y.toFixed(1)}" r="3.6" fill="${col}" ${da}/>`;
      // larger transparent hit target for easy hover
      svg += `<circle class="hit" cx="${X.toFixed(1)}" cy="${Y.toFixed(1)}" r="11" ${da}>`
        + `<title>${esc(os)} &middot; ${esc(metricKey)} = ${esc(fmt(p.y, " "+(unit||"")).trim())} &middot; ${esc(p.r.mode||"?")}${p.r.ts?" &middot; "+esc(p.r.ts):""}</title></circle>`;
    });
    // always-visible value label on the most recent point of this series
    if (pts.length){
      const last = pts[pts.length-1];
      const X = sx(pts.length-1), Y = sy(last.y);
      const anchor = (pts.length-1) >= maxX ? "end" : "middle";
      const lx = anchor === "end" ? X - 6 : X;
      svg += `<text class="pt-label" x="${lx.toFixed(1)}" y="${(Y-8).toFixed(1)}" text-anchor="${anchor}" fill="${col}">${esc(fmt(last.y, unit))}</text>`;
    }
  }
  svg += `</svg>`;
  // legend
  let legend = `<div class="legend">`;
  for (const os of byOs.keys())
    legend += `<span><b style="background:${osColor(os)}"></b>${esc(os)}</span>`;
  legend += `</div>`;
  return svg + legend;
}

let currentMetric = "build_s";
function renderCharts(){
  // metric selector buttons
  const ctl = document.getElementById("metricControls");
  ctl.innerHTML = "";
  const allBtn = document.createElement("button");
  allBtn.textContent = "All";
  allBtn.className = currentMetric === "*" ? "active" : "";
  allBtn.onclick = () => { currentMetric = "*"; renderCharts(); };
  ctl.appendChild(allBtn);
  for (const m of METRICS){
    const b = document.createElement("button");
    b.textContent = m.key;
    b.className = currentMetric === m.key ? "active" : "";
    b.onclick = () => { currentMetric = m.key; renderCharts(); };
    ctl.appendChild(b);
  }
  const charts = document.getElementById("charts");
  charts.innerHTML = "";
  const show = currentMetric === "*" ? METRICS : METRICS.filter(m=>m.key===currentMetric);
  for (const m of show){
    const div = document.createElement("div");
    div.className = "chart";
    div.innerHTML = `<h3>${esc(m.label)}</h3>` + lineChart(m.key, m.unit);
    charts.appendChild(div);
    wireChartTooltip(div);
  }
}

// Prominent, legible hover tooltip for chart dots (file:// safe — pure JS).
function wireChartTooltip(chartDiv){
  const dots = chartDiv.querySelectorAll("circle.dot, circle.hit");
  if (!dots.length) return;
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  chartDiv.appendChild(tip);
  const show = (ev) => {
    const c = ev.currentTarget;
    const os = c.getAttribute("data-os") || "?";
    const metric = c.getAttribute("data-metric") || "";
    const val = c.getAttribute("data-val") || "";
    const mode = c.getAttribute("data-mode") || "?";
    const ts = c.getAttribute("data-ts") || "";
    const col = c.getAttribute("data-col") || "var(--accent)";
    tip.style.borderColor = col;
    tip.innerHTML =
      `<div class="tip-os" style="color:${col}">${esc(os)}</div>`
      + `<div><span class="tip-val">${esc(metric)} = ${esc(val)}</span></div>`
      + `<div class="tip-meta">mode: ${esc(mode)}${ts?" &middot; "+esc(ts):""}</div>`;
    tip.style.display = "block";
    position(ev);
  };
  const position = (ev) => {
    const box = chartDiv.getBoundingClientRect();
    let x = ev.clientX - box.left + 12;
    let y = ev.clientY - box.top + 12;
    // keep inside the chart card
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    if (x + tw > box.width) x = box.width - tw - 6;
    if (y + th > box.height) y = ev.clientY - box.top - th - 12;
    if (x < 0) x = 4; if (y < 0) y = 4;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  };
  const hide = () => { tip.style.display = "none"; };
  dots.forEach(c => {
    c.addEventListener("mouseenter", show);
    c.addEventListener("mousemove", position);
    c.addEventListener("mouseleave", hide);
  });
}

// ---- Session list + detail readout ----
const FIELD_ORDER = ["ts","os","arch","provider","mode","git_sha",
  "vm_clone_s","configure_s","build_s","ctest_s",
  "ccache_hit_pct","cache_size_gb",
  "tests_total","tests_passed","tests_failed","note"];
function renderRunList(){
  const list = document.getElementById("runlist");
  if (!DATA.length){ list.innerHTML = `<div class="empty">No runs.</div>`; return; }
  // most recent first by original order
  const ordered = [...DATA].sort((a,b)=>(b._idx||0)-(a._idx||0));
  list.innerHTML = "";
  for (const r of ordered){
    const div = document.createElement("div");
    div.className = "runrow";
    div.dataset.idx = r._idx;
    const b = num(r,"build_s");
    div.innerHTML = `<span>${esc(r.ts||"?")} &middot; <b style="color:${osColor(r.os)}">${esc(r.os||"?")}</b>`
      + ` <span class="badge">${esc(r.mode||"?")}</span></span>`
      + `<small>build ${fmt(b,"s")}</small>`;
    div.onclick = () => selectRun(r._idx);
    list.appendChild(div);
  }
}
function selectRun(idx){
  const row = DATA.find(r => r._idx === idx);
  document.querySelectorAll(".runrow").forEach(el =>
    el.classList.toggle("sel", String(idx) === el.dataset.idx));
  const d = document.getElementById("detail");
  if (!row){ d.innerHTML = `<div class="empty">Run not found.</div>`; return; }
  let h = `<h3>${esc(row.ts||"?")} &mdash; ${esc(row.os||"?")} / ${esc(row.mode||"?")}</h3>`;
  h += `<div class="kv">`;
  const seen = new Set(["_idx","ctest_label_times"]);
  // ordered known fields first
  for (const k of FIELD_ORDER){
    if (k in row){ seen.add(k);
      h += `<div class="k">${esc(k)}</div><div class="v">${esc(row[k])}</div>`; }
  }
  // any other fields not in known order (forward-compat)
  for (const k of Object.keys(row)){
    if (seen.has(k) || k === "ctest_label_times") continue;
    h += `<div class="k">${esc(k)}</div><div class="v">${esc(JSON.stringify(row[k]))}</div>`;
  }
  h += `</div>`;
  // ctest_label_times sub-table if present
  const lt = row.ctest_label_times;
  if (lt && typeof lt === "object" && Object.keys(lt).length){
    h += `<table class="subtable"><thead><tr><th>ctest label</th><th>seconds</th></tr></thead><tbody>`;
    for (const [lab, sec] of Object.entries(lt).sort((a,b)=>(b[1]||0)-(a[1]||0)))
      h += `<tr><td>${esc(lab)}</td><td>${esc(sec)}</td></tr>`;
    h += `</tbody></table>`;
  }
  d.innerHTML = h;
}

wireInfoIcon();
renderLastRun();
renderRecent();
renderSummary();
renderCharts();
renderRunList();
</script>
</body>
</html>
"""


def render_html(rows):
    data_json = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    out = PAGE_TEMPLATE.replace("__DATA__", data_json)
    out = out.replace("__THRESHOLD__", repr(TREND_THRESHOLD))
    return out


def main():
    in_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT
    out_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUTPUT

    if not os.path.exists(in_path):
        sys.stderr.write("error: metrics file not found: %s\n" % in_path)
        return 1

    rows = load_rows(in_path)

    # stdout text report (works headless)
    print(text_report(rows))

    html_out = render_html(rows)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_out)

    print("")
    print("wrote %s (%d runs, %d bytes)" % (out_path, len(rows), len(html_out)))
    print("open: file://%s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
