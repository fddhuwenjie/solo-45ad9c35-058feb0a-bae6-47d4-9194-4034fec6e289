/* 复测对照工作区 — 前端(原生 JS + SVG)
 * 基准/复测轮次配对结果在平面图与曲线区切换或叠放;
 * 人工改配、确认锁定、成片退化反查、三类导出。
 */
"use strict";

const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

const MIG_COLOR = { "ok->fail": "#d24040", "fail->ok": "#2e9e5b",
                    "ok->ok": "#1c6dd9", "fail->fail": "#d8a012" };
const NC_COLOR = "#9b59b6";
const MIG_SHORT = { "ok->fail": "退化", "fail->ok": "改善",
                    "ok->ok": "保持合格", "fail->fail": "保持不合格" };
const METHOD_TEXT = { label: "点号", proximity: "位置", manual: "人工" };
const EVENT_TEXT = { create: "创建对照", conditions: "更新工况/参数",
                     override: "人工改配", "override-delete": "删除改配",
                     confirm: "确认结果", reopen: "重审解锁" };

let projectId = null;
let versions = [];       // 项目全部轮次
let C = null;            // 当前对照状态
let basePts = {};        // 基准轮测点 label -> point
let retestPts = {};      // 复测轮测点 label -> point
let viewMode = "overlay";// overlay | base | retest
let selPair = null;      // 选中配对 key
let selCluster = null;   // 选中退化区 id
let pan = null;

const viewport = $("#viewport");
const spectrum = $("#spectrum");

/* ---------------------------------------------------------------- 工具 */

function el(name, attrs, parent) {
  const e = document.createElementNS(SVGNS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function status(msg) { $("#statusbar").textContent = msg; }

async function api(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { status("错误:" + (data.error || r.status)); throw new Error(data.error || r.status); }
  return data;
}

function svgPoint(clientX, clientY) {
  const pt = viewport.createSVGPoint();
  pt.x = clientX; pt.y = clientY;
  return pt.matrixTransform(viewport.getScreenCTM().inverse());
}

function pairColor(p) {
  if (p.status === "noconclusion") return NC_COLOR;
  return MIG_COLOR[p.migration.overall] || "#888";
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------------------------------------------------------- 初始化 */

async function boot() {
  const projects = await api("/api/projects");
  if (!projects.length) { $("#noProject").classList.remove("hidden"); return; }
  const q = new URLSearchParams(location.search);
  projectId = +(q.get("project") || projects[0].id);
  const st = await api("/api/projects/" + projectId + "/state");
  versions = st.versions;
  $("#projName").textContent = st.project.name;
  $("#main").classList.remove("hidden");
  fillVersionSelects();
  await refreshCmpList();
}

function fillVersionSelects() {
  for (const sel of [$("#selBase"), $("#selRetest")]) {
    sel.innerHTML = "";
    for (const v of [...versions].reverse()) {
      const o = document.createElement("option");
      o.value = v.id;
      o.textContent = "#" + v.id + " " + v.label;
      sel.appendChild(o);
    }
  }
  if (versions.length >= 2) {
    $("#selBase").value = versions[versions.length - 1].id;   // 较早一轮作基准
    $("#selRetest").value = versions[0].id;
  }
}

async function refreshCmpList(selectId) {
  const list = await api("/api/projects/" + projectId + "/comparisons");
  const sel = $("#cmpSelect");
  sel.innerHTML = "";
  if (!list.length) {
    const o = document.createElement("option");
    o.textContent = "(尚无对照,请在左栏新建)";
    sel.appendChild(o);
    C = null; renderAll();
    return;
  }
  for (const c of list) {
    const o = document.createElement("option");
    o.value = c.id;
    o.textContent = "#" + c.id + " " + c.label +
      " [" + c.base_label + " ↔ " + c.retest_label + "]" +
      (c.status === "confirmed" ? (c.stale ? " 已确认·待重审" : " 已确认") : " 草稿");
    sel.appendChild(o);
  }
  const target = selectId || list[0].id;
  sel.value = target;
  await loadComparison(+sel.value);
}

async function loadComparison(cid) {
  C = await api("/api/comparisons/" + cid);
  selPair = null; selCluster = null;
  const [bs, rs] = await Promise.all([
    api("/api/projects/" + projectId + "/state?version_id=" + C.base_version_id),
    api("/api/projects/" + projectId + "/state?version_id=" + C.retest_version_id),
  ]);
  basePts = {}; retestPts = {};
  for (const p of bs.points) basePts[p.label] = p;
  for (const p of rs.points) retestPts[p.label] = p;
  renderAll();
}

/* ---------------------------------------------------------------- 总渲染 */

function renderAll() {
  renderHeader();
  renderSettings();
  renderOverrides();
  renderEvents();
  renderStats();
  renderPairList();
  renderClusters();
  renderViewport();
  renderPairDetail();
  drawSpectrum();
  renderLegend();
  $("#rawBox").innerHTML = "<p class='dim'>选择配对或退化区后反查</p>";
}

function renderHeader() {
  const b = $("#cmpBadge");
  if (!C) { b.textContent = ""; return; }
  b.textContent = C.status === "confirmed"
    ? (C.stale ? "已确认 · 待重审" : "已确认") : "草稿";
  b.classList.toggle("mixed", C.status === "confirmed" && C.stale);
  $("#btnConfirm").classList.toggle("hidden", C.status !== "draft");
  $("#btnReopen").classList.toggle("hidden", C.status !== "confirmed");
  const confirmed = C.status === "confirmed";
  for (const id of ["#btnExpSvg", "#btnExpCsv", "#btnExpJson"]) {
    $(id).disabled = !confirmed;
    $(id).title = confirmed ? "导出(取自确认结果)" : "确认结果后才能导出(三份材料同源)";
  }
  $("#btnSaveCond").disabled = confirmed;
  $("#btnOverride").disabled = confirmed;
  const bar = $("#staleBar");
  if (C.stale) {
    bar.textContent = "⚠ 来源轮次在确认后派生了新修订,结论可能已过时,请重审(重审后可改配并重新确认)。";
    bar.classList.remove("hidden");
  } else bar.classList.add("hidden");
}

function renderSettings() {
  if (!C) return;
  $("#cmpLabel").value = C.label || "";
  $("#posTol").value = C.pos_tol;
  $("#selBase").value = C.base_version_id;
  $("#selRetest").value = C.retest_version_id;
  $("#baseOcc").value = C.conditions.base_occ || "";
  $("#baseLighting").value = C.conditions.base_lighting || "";
  $("#basePa").value = C.conditions.base_pa || "";
  $("#retestOcc").value = C.conditions.retest_occ || "";
  $("#retestLighting").value = C.conditions.retest_lighting || "";
  $("#retestPa").value = C.conditions.retest_pa || "";
}

/* ---------------------------------------------------------------- 平面图 */

function renderViewport() {
  viewport.innerHTML = "";
  if (!C || !C.result) return;
  const b = C.project_bounds;
  const mx = b.width * 0.06, my = b.height * 0.06;
  viewport.setAttribute("viewBox", [b.min_x - mx, b.min_y - my,
    b.width + 2 * mx, b.height + 2 * my].join(" "));

  const gVenue = el("g", { opacity: 0.5 }, viewport);
  try {
    const doc = new DOMParser().parseFromString(C.venue_svg, "image/svg+xml");
    const root = doc.querySelector("svg");
    if (root) for (const n of [...root.childNodes]) gVenue.appendChild(document.importNode(n, true));
  } catch (e) { /* 底图解析失败不阻断 */ }

  const gLink = el("g", {}, viewport);
  const gMark = el("g", {}, viewport);
  const R = C.result;

  // 成片退化区轮廓
  for (const cl of R.clusters) {
    if (!cl.clustered) continue;
    const c = el("circle", { cx: cl.x, cy: cl.y, r: 2.0 + 1.2 * cl.n,
      fill: "rgba(210,64,64,.08)", stroke: "#d24040", "stroke-width": 0.3,
      "stroke-dasharray": "1.4 0.9", cursor: "pointer" }, gMark);
    if (selCluster === cl.id) { c.setAttribute("stroke", "#fff"); c.setAttribute("stroke-width", 0.5); }
    el("title", {}, c).textContent = "成片退化席位区 #" + cl.id + ":" + cl.n + " 个位置";
    c.addEventListener("click", (ev) => { ev.stopPropagation(); selectCluster(cl.id); });
  }

  const showBase = viewMode !== "retest", showRetest = viewMode !== "base";
  for (const p of R.pairs) {
    const color = pairColor(p);
    if (viewMode === "overlay" && p.dist && p.dist > 0.3)
      el("line", { x1: p.x, y1: p.y, x2: p.rx, y2: p.ry, stroke: color,
        "stroke-width": 0.18, "stroke-dasharray": "0.6 0.5" }, gLink);
    if (showRetest && viewMode === "overlay" && p.dist && p.dist > 0.3)
      el("circle", { cx: p.rx, cy: p.ry, r: 0.4, fill: "none",
        stroke: color, "stroke-width": 0.15, "stroke-dasharray": "0.4 0.3" }, gMark);
    const cx = viewMode === "retest" ? p.rx : p.x;
    const cy = viewMode === "retest" ? p.ry : p.y;
    const grp = el("g", { cursor: "pointer" }, gMark);
    el("circle", { cx, cy, r: selPair === p.key ? 0.95 : 0.62, fill: color,
      stroke: selPair === p.key ? "#fff" : "rgba(255,255,255,.75)",
      "stroke-width": selPair === p.key ? 0.3 : 0.15 }, grp);
    if (p.method === "manual")
      el("circle", { cx, cy, r: 1.05, fill: "none", stroke: "#ffd75e", "stroke-width": 0.25 }, grp);
    const t = el("title", {}, grp);
    t.textContent = p.base_label + " ↔ " + p.retest_label + " " +
      (p.status === "noconclusion" ? p.reason : MIG_SHORT[p.migration.overall]) +
      (p.manual_note ? " [改配:" + p.manual_note + "]" : "");
    grp.addEventListener("click", (ev) => { ev.stopPropagation(); selectPair(p.key); });
  }

  // 未配对与多重匹配测点
  const drawLonely = (label, pts, shape) => {
    const p = pts[label];
    if (!p) return;
    const pos = viewMode === "retest" ? (retestPts[label] || p) : p;
    if (shape === "r" && !showRetest) return;
    if (shape === "b" && !showBase) return;
    const g2 = el("g", {}, gMark);
    el("rect", { x: pos.x - 0.45, y: pos.y - 0.45, width: 0.9, height: 0.9,
      fill: "none", stroke: "#8a8f98", "stroke-width": 0.18,
      "stroke-dasharray": "0.4 0.3" }, g2);
    el("title", {}, g2).textContent = label + "(仅" + (shape === "b" ? "基准" : "复测") + "轮,未配对)";
  };
  for (const lb of R.unpaired.base_only) drawLonely(lb, basePts, "b");
  for (const lb of R.unpaired.retest_only) drawLonely(lb, retestPts, "r");
  for (const a of R.ambiguous) {
    const p = (a.side === "base" ? basePts : retestPts)[a.label];
    if (!p) continue;
    const g2 = el("g", {}, gMark);
    el("rect", { x: p.x - 0.5, y: p.y - 0.5, width: 1, height: 1, fill: "none",
      stroke: NC_COLOR, "stroke-width": 0.22 }, g2);
    el("title", {}, g2).textContent = a.label + " 多重匹配,候选:" + a.candidates.join("/");
  }
}

function renderLegend() {
  const items = [["#d24040", "退化"], ["#2e9e5b", "改善"], ["#1c6dd9", "保持合格"],
                 ["#d8a012", "保持不合格"], [NC_COLOR, "无结论"], ["#8a8f98", "未配对"]];
  $("#legend").innerHTML = items.map(i =>
    "<div class='item'><span class='sw' style='background:" + i[0] + "'></span>" + i[1] + "</div>").join("");
}

/* ---------------------------------------------------------------- 侧栏 */

function renderStats() {
  const box = $("#stats");
  if (!C || !C.result) { box.innerHTML = "<span class='dim'>尚未选择对照</span>"; return; }
  const s = C.result.stats;
  const chips = [
    ["#1c6dd9", "配对 " + s.paired], ["#2e9e5b", "改善 " + s.improved],
    ["#d24040", "退化 " + s.degraded], [NC_COLOR, "无结论 " + s.noconclusion],
    ["#8a8f98", "多重匹配 " + s.ambiguous],
    ["#8a8f98", "仅基准 " + s.base_only], ["#8a8f98", "仅复测 " + s.retest_only],
    ["#d24040", "成片退化区 " + s.degraded_clusters],
  ];
  box.innerHTML = "<div class='chips'>" + chips.map(c =>
    "<span class='chip' style='background:" + c[0] + "'>" + c[1] + "</span>").join("") + "</div>";
}

function renderPairList() {
  const ul = $("#pairList");
  ul.innerHTML = "";
  if (!C || !C.result) return;
  const R = C.result;
  $("#pairCount").textContent = "(" + R.pairs.length + ")";
  for (const p of R.pairs) {
    const li = document.createElement("li");
    if (p.key === selPair) li.classList.add("sel");
    const txt = p.status === "noconclusion" ? "无结论" : MIG_SHORT[p.migration.overall];
    li.innerHTML = "<span class='dot' style='background:" + pairColor(p) + "'></span>" +
      esc(p.base_label) + "↔" + esc(p.retest_label) +
      (p.method === "manual" ? "<span class='tag'>人工</span>" : "") +
      "<span class='dim' style='margin-left:auto'>" + txt + "</span>";
    li.title = p.reason || "";
    li.onclick = () => selectPair(p.key);
    ul.appendChild(li);
  }
  for (const a of R.ambiguous) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='dot' style='background:" + NC_COLOR + "'></span>" +
      esc(a.label) + "<span class='tag'>多重匹配</span>" +
      "<span class='dim' style='margin-left:auto'>" + esc(a.candidates.join("/")) + "</span>";
    ul.appendChild(li);
  }
}

function renderOverrides() {
  const ul = $("#overrideList");
  ul.innerHTML = "";
  if (!C) return;
  if (!C.overrides.length) ul.innerHTML = "<li class='dim'>暂无人工改配</li>";
  for (const o of C.overrides) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='tag'>改配</span>" + esc(o.base_label) + " → " +
      (o.retest_label ? esc(o.retest_label) : "(取消配对)") +
      "<span class='dim'> " + esc(o.note) + "</span>" +
      (C.status === "draft" ? "<span class='del' title='删除'>✕</span>" : "");
    const del = li.querySelector(".del");
    if (del) del.onclick = async () => {
      C = await api("/api/comparisons/" + C.id + "/overrides/" + o.id, { method: "DELETE" });
      status("改配已删除,结果已重算"); renderAll();
    };
    ul.appendChild(li);
  }
}

function renderEvents() {
  const ul = $("#eventList");
  ul.innerHTML = "";
  if (!C) return;
  if (!C.events.length) ul.innerHTML = "<li class='dim'>暂无事件</li>";
  for (const e of C.events) {
    const li = document.createElement("li");
    let txt = EVENT_TEXT[e.kind] || e.kind;
    if (e.kind === "override") txt += " " + e.payload.base_label + "→" + (e.payload.retest_label || "取消");
    li.innerHTML = "<span class='tag'>" + txt + "</span><span class='dim'>" + e.created_at + "</span>";
    if (e.kind === "override" && e.payload.note) li.title = e.payload.note;
    ul.appendChild(li);
  }
}

function renderClusters() {
  const ul = $("#clusterList");
  ul.innerHTML = "";
  if (!C || !C.result) return;
  const cls = C.result.clusters;
  if (!cls.length) { ul.innerHTML = "<li class='dim'>无退化位置</li>"; return; }
  for (const cl of cls) {
    const li = document.createElement("li");
    if (cl.id === selCluster) li.classList.add("sel");
    li.innerHTML = "<span class='dot' style='background:#d24040'></span>区 #" + cl.id +
      " (" + cl.n + " 位置)" + (cl.clustered ? "<span class='tag'>成片</span>" : "") +
      "<span class='dim' style='margin-left:auto'>" + cl.labels.slice(0, 4).join(",") +
      (cl.labels.length > 4 ? "…" : "") + "</span>";
    li.onclick = () => selectCluster(cl.id);
    ul.appendChild(li);
  }
}

/* ---------------------------------------------------------------- 配对详情与曲线 */

function curPair() {
  return C && C.result ? C.result.pairs.find(p => p.key === selPair) : null;
}

function selectPair(key) {
  selPair = key; selCluster = null;
  renderPairList(); renderPairDetail(); drawSpectrum(); renderViewport(); renderClusters();
}

function selectCluster(id) {
  selCluster = selCluster === id ? null : id;
  renderClusters(); renderViewport();
  const cl = C.result.clusters.find(c => c.id === selCluster);
  if (cl) status("退化区 #" + cl.id + ":" + cl.n + " 个位置 — 可点击“反查选中配对/区域”查看两轮原始记录");
}

function renderPairDetail() {
  const box = $("#pairDetail");
  const p = curPair();
  $("#pairTitle").textContent = p ? p.base_label + " ↔ " + p.retest_label : "";
  if (!p) { box.innerHTML = "<p class='dim'>在平面图或配对列表中点击选择</p>"; return; }
  let html = "<table>";
  html += "<tr><td>配对方式</td><td>" + (METHOD_TEXT[p.method] || p.method) +
    (p.dist != null ? " · 位置偏差 " + p.dist + " m" : "") + "</td></tr>";
  html += "<tr><td>设备/校准</td><td>" + esc(p.base_device || "—") + " " + esc(p.base_calib || "—") +
    " ↔ " + esc(p.retest_device || "—") + " " + esc(p.retest_calib || "—") + "</td></tr>";
  if (p.manual_note) html += "<tr><td>改配备注</td><td>" + esc(p.manual_note) + "</td></tr>";
  if (p.status === "noconclusion") {
    html += "</table><p class='issue'>⚠ 无结论:" + esc(p.reason) + "</p>";
    box.innerHTML = html;
    return;
  }
  const m = p.metrics;
  html += "<tr><td>场强@" + m.ref_freq + "Hz</td><td>" + m.base.field + " → " + m.retest.field +
    " dB(Δ" + (m.delta.field > 0 ? "+" : "") + m.delta.field + ")</td></tr>";
  html += "<tr><td>信噪比</td><td>" + m.base.snr + " → " + m.retest.snr +
    " dB(Δ" + (m.delta.snr > 0 ? "+" : "") + m.delta.snr + ")</td></tr>";
  html += "<tr><td>频响偏差</td><td>" + m.base.freq_dev + " → " + m.retest.freq_dev +
    " dB(Δ" + (m.delta.freq_dev > 0 ? "+" : "") + m.delta.freq_dev + ")</td></tr>";
  html += "</table>";
  html += "<div class='chips' style='margin:4px 0'>" +
    ["field", "snr", "freq_dev", "overall"].map(k =>
      "<span class='chip' style='background:" + (MIG_COLOR[p.migration[k]] || "#666") + "'>" +
      { field: "场强", snr: "信噪比", freq_dev: "频响", overall: "综合" }[k] + " " +
      MIG_SHORT[p.migration[k]] + "</span>").join("") + "</div>";
  html += "<table><tr><td>频率</td><td>Δ读数</td><td>Δ场强余量</td><td>Δ信噪余量</td></tr>";
  for (const f of p.freqs)
    html += "<tr><td>" + f.freq + " Hz</td><td>" + (f.delta_field > 0 ? "+" : "") + f.delta_field +
      " dB</td><td>" + (f.delta_field_margin > 0 ? "+" : "") + f.delta_field_margin +
      " dB</td><td>" + (f.delta_snr_margin > 0 ? "+" : "") + f.delta_snr_margin + " dB</td></tr>";
  html += "</table>";
  box.innerHTML = html;
}

function drawSpectrum() {
  spectrum.innerHTML = "";
  const W = 340, H = 230, L = 38, R = 8, T = 14, B = 26;
  const p = curPair();
  const bp = p && basePts[p.base_label], rp = p && retestPts[p.retest_label];
  const lim = C && C.result ? C.result.limits_snapshot : null;
  const freqs = p && p.freqs && p.freqs.length ? p.freqs.map(f => f.freq)
    : [100, 500, 1000, 2000, 4000, 5000];
  const fmin = Math.min(...freqs) / 1.4, fmax = Math.max(...freqs) * 1.4;
  const xs = (f) => L + (Math.log10(f) - Math.log10(fmin)) / (Math.log10(fmax) - Math.log10(fmin)) * (W - L - R);

  let lo = -45, hi = 5;
  if (bp && rp) {
    const vals = [...Object.values(bp.freqs), ...Object.values(rp.freqs)]
      .flatMap(v => [v.field, v.noise]);
    lo = Math.min(lo, ...vals) - 3; hi = Math.max(hi, ...vals) + 3;
  }
  const ys = (db) => T + (hi - db) / (hi - lo) * (H - T - B);

  for (let db = Math.ceil(lo / 10) * 10; db <= hi; db += 10) {
    el("line", { x1: L, y1: ys(db), x2: W - R, y2: ys(db), class: "grid" }, spectrum);
    const t = el("text", { x: 4, y: ys(db) + 3 }, spectrum); t.textContent = db;
  }
  for (const f of freqs) {
    el("line", { x1: xs(f), y1: T, x2: xs(f), y2: H - B, class: "grid" }, spectrum);
    const t = el("text", { x: xs(f), y: H - B + 12, "text-anchor": "middle" }, spectrum);
    t.textContent = f >= 1000 ? (f / 1000) + "k" : f;
  }
  el("line", { x1: L, y1: H - B, x2: W - R, y2: H - B, class: "axis" }, spectrum);
  el("line", { x1: L, y1: T, x2: L, y2: H - B, class: "axis" }, spectrum);
  const cap = el("text", { x: W - R, y: H - 4, "text-anchor": "end" }, spectrum);
  cap.textContent = "Hz / dB  绿实:基准 橙虚:复测 灰:噪声 红:限值";

  if (lim) {
    el("rect", { x: L, y: ys(lim.field_max), width: W - L - R,
      height: ys(lim.field_min) - ys(lim.field_max), class: "band" }, spectrum);
    for (const v of [lim.field_min, lim.field_max])
      el("line", { x1: L, y1: ys(v), x2: W - R, y2: ys(v), class: "limit-line" }, spectrum);
  }
  if (!p || !bp || !rp) {
    const t = el("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, spectrum);
    t.textContent = p ? "该配对无逐频结论" : "选择配对后叠加两轮频响";
    return;
  }
  const line = (pts, key, cls) => {
    const fk = Object.keys(pts.freqs).map(Number).sort((a, b) => a - b);
    el("polyline", { points: fk.map(f => xs(f) + "," + ys(pts.freqs[f][key])).join(" "),
      class: cls }, spectrum);
  };
  line(bp, "noise", "noise-line"); line(rp, "noise", "noise-line");
  line(bp, "field", "field-line"); line(rp, "field", "retest-line");
  for (const f of p.freqs || []) {
    el("circle", { cx: xs(f.freq), cy: ys(f.base_field), r: 3.2, class: "base-dot" }, spectrum);
    el("circle", { cx: xs(f.freq), cy: ys(f.retest_field), r: 3.2, class: "retest-dot" }, spectrum);
    const dt = el("text", { x: xs(f.freq), y: ys(Math.max(f.base_field, f.retest_field)) - 5,
      "text-anchor": "middle", class: "delta" }, spectrum);
    dt.textContent = (f.delta_field > 0 ? "+" : "") + f.delta_field;
  }
}

/* ---------------------------------------------------------------- 原始记录反查 */

async function fetchRaw() {
  if (!C) return;
  let labels = [];
  if (selCluster) {
    const cl = C.result.clusters.find(c => c.id === selCluster);
    if (cl) labels = cl.labels;
  } else if (selPair) {
    const p = curPair();
    if (p) labels = [p.base_label, p.retest_label];
  }
  labels = [...new Set(labels)];
  if (!labels.length) { status("请先选择配对或退化区"); return; }
  const data = await api("/api/comparisons/" + C.id + "/raw?labels=" + labels.join(","));
  renderRaw(data);
}

function renderRaw(data) {
  const box = $("#rawBox");
  const tbl = (title, rowsData, vid) => {
    let h = "<h4>" + title + "(版本 #" + vid + ")</h4>";
    if (!rowsData.length) return h + "<p class='dim'>无记录</p>";
    h += "<table><tr><td>测点</td><td>Hz</td><td>场强</td><td>噪声</td><td>设备/校准</td></tr>";
    for (const r of rowsData)
      h += "<tr" + (r.excluded ? " class='dim' title='已排除:" + esc(r.exclude_reason) + "'" : "") +
        "><td>" + esc(r.point_label) + "</td><td>" + r.freq_hz + "</td><td>" + r.field_db +
        "</td><td>" + r.noise_db + "</td><td>" + esc(r.device_id || "—") + "/" +
        esc(r.calib_version || "—") + "</td></tr>";
    return h + "</table>";
  };
  box.innerHTML = tbl("基准轮", data.base, data.base_version_id) +
                  tbl("复测轮", data.retest, data.retest_version_id);
}

/* ---------------------------------------------------------------- 操作 */

$("#btnCreate").onclick = async () => {
  const body = {
    base_version_id: +$("#selBase").value, retest_version_id: +$("#selRetest").value,
    label: $("#cmpLabel").value, pos_tol: +$("#posTol").value || 1.0,
    base_occ: $("#baseOcc").value, base_lighting: $("#baseLighting").value,
    base_pa: $("#basePa").value, retest_occ: $("#retestOcc").value,
    retest_lighting: $("#retestLighting").value, retest_pa: $("#retestPa").value,
  };
  const nc = await api("/api/projects/" + projectId + "/comparisons",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  status("对照 #" + nc.id + " 已创建并完成自动配对");
  await refreshCmpList(nc.id);
};

$("#btnSaveCond").onclick = async () => {
  if (!C) return;
  C = await api("/api/comparisons/" + C.id + "/conditions", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      label: $("#cmpLabel").value, pos_tol: +$("#posTol").value || C.pos_tol,
      base_occ: $("#baseOcc").value, base_lighting: $("#baseLighting").value,
      base_pa: $("#basePa").value, retest_occ: $("#retestOcc").value,
      retest_lighting: $("#retestLighting").value, retest_pa: $("#retestPa").value,
    }),
  });
  status("工况与参数已保存,配对结果已重算");
  renderAll(); refreshCmpList(C.id);
};

$("#btnOverride").onclick = async () => {
  if (!C) return;
  C = await api("/api/comparisons/" + C.id + "/overrides", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_label: $("#ovBase").value.trim(),
      retest_label: $("#ovRetest").value.trim(), note: $("#ovNote").value.trim() }),
  });
  $("#ovBase").value = $("#ovRetest").value = $("#ovNote").value = "";
  status("人工改配已记录,结果已重算");
  renderAll();
};

$("#btnConfirm").onclick = async () => {
  if (!C) return;
  C = await api("/api/comparisons/" + C.id + "/confirm", { method: "POST" });
  status("对照已确认:来源轮次与配对表已锁定,可导出三份材料");
  renderAll(); refreshCmpList(C.id);
};

$("#btnReopen").onclick = async () => {
  if (!C) return;
  C = await api("/api/comparisons/" + C.id + "/reopen", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: "来源修订后重审" }),
  });
  status("已解除锁定进入重审;调整配对或参数后请重新确认");
  renderAll(); refreshCmpList(C.id);
};

$("#btnRaw").onclick = fetchRaw;
$("#cmpSelect").onchange = (ev) => loadComparison(+ev.target.value);
$("#btnExpSvg").onclick = () => { if (C) location = "/api/comparisons/" + C.id + "/export/diff.svg"; };
$("#btnExpCsv").onclick = () => { if (C) location = "/api/comparisons/" + C.id + "/export/detail.csv"; };
$("#btnExpJson").onclick = () => { if (C) location = "/api/comparisons/" + C.id + "/export/recalc.json"; };

document.querySelectorAll("#viewMode button").forEach(b => b.onclick = () => {
  viewMode = b.dataset.mode;
  document.querySelectorAll("#viewMode button").forEach(x => x.classList.toggle("active", x === b));
  renderViewport();
});

/* 平移 / 缩放 */
viewport.addEventListener("mousedown", (ev) => {
  if (ev.target === viewport) {
    pan = { sx: ev.clientX, sy: ev.clientY, vb: { ...viewport.viewBox.baseVal } };
    viewport.classList.add("panning");
  }
});
window.addEventListener("mousemove", (ev) => {
  if (!pan) return;
  const vb = viewport.viewBox.baseVal;
  const k = vb.width / viewport.clientWidth;
  vb.x = pan.vb.x - (ev.clientX - pan.sx) * k;
  vb.y = pan.vb.y - (ev.clientY - pan.sy) * k;
});
window.addEventListener("mouseup", () => { pan = null; viewport.classList.remove("panning"); });
viewport.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const vb = viewport.viewBox.baseVal;
  const p = svgPoint(ev.clientX, ev.clientY);
  const k = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
  vb.x = p.x - (p.x - vb.x) * k;
  vb.y = p.y - (p.y - vb.y) * k;
  vb.width *= k; vb.height *= k;
}, { passive: false });
viewport.addEventListener("click", (ev) => {
  if (ev.target === viewport) {
    selPair = null; selCluster = null;
    renderPairList(); renderPairDetail(); drawSpectrum(); renderViewport(); renderClusters();
  }
});

boot();
