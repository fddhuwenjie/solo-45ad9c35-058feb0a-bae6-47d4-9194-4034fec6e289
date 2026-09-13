/* 边界外逸工作区 — 前端(原生 JS + SVG)
 * 平面图:本环服务区/相邻环区/保密边界绘制,开/关测点配对展示,
 *         误定位点拖动、边界顶点拖动与拆分;
 * 里程曲线:沿线外逸量/开启场强,与平面图测站联动。
 * 人工改配、调边界必须备注并形成新修订;确认后锁定来源测次、配对表与限值。
 */
"use strict";

const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

const ST_COLOR = { ok: "#2e9e5b", fail: "#d24040", noconclusion: "#9b59b6",
                   nodata: "#8a8f98", internal: "#4da3ff" };
const ST_TEXT = { ok: "合格", fail: "超限", noconclusion: "无结论",
                  nodata: "无数据", internal: "本环区内" };
const ZONE_COLOR = { own: "#4da3ff", adjacent: "#f0932b" };
const ZONE_TEXT = { own: "本环服务区", adjacent: "相邻环区" };
const METHOD_TEXT = { label: "点号", proximity: "位置", manual: "人工" };
const EVENT_TEXT = {
  create: "创建校审", "import-run": "导入测次", "delete-run": "删除测次",
  move: "移动测点", "zone-add": "添加环区", "zone-delete": "删除环区",
  "path-add": "添加边界", "path-delete": "删除边界",
  "path-edit": "调整边界顶点", "path-split": "拆分边界",
  override: "人工改配", "override-delete": "撤销改配",
  params: "调整参数", confirm: "确认锁定", reopen: "重审新修订",
};

let projectId = null;
let surveys = [];
let L = null;               // 当前校审状态
let tool = "select";
let draft = [];             // 正在画的顶点
let viewMode = "both";
let selPathId = null;       // 里程曲线所选边界
let selS = null;            // 选中测站里程
let drag = null;
let pan = null;
let pendingImport = null;   // "on" | "off"

const viewport = $("#viewport");
const profile = $("#profile");

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

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function ncReason(code) {
  const [k, arg] = code.split(":");
  const t = (L && L.nc_text[k]) || k;
  return arg ? t + "(" + arg + ")" : t;
}

function result() { return L && L.result; }
function paths() { const r = result(); return r ? r.paths : []; }
function selPath() { return paths().find(p => p.id === selPathId) || paths()[0] || null; }
function locked() { return L && L.status === "confirmed"; }

/* ---------------------------------------------------------------- 初始化 */

async function boot() {
  const projects = await api("/api/projects");
  if (!projects.length) { $("#noProject").classList.remove("hidden"); return; }
  const q = new URLSearchParams(location.search);
  projectId = +(q.get("project") || projects[0].id);
  $("#projName").textContent = projects.find(p => p.id === projectId)?.name || "";
  $("#main").classList.remove("hidden");
  await refreshSurveyList();
}

async function refreshSurveyList(selectId) {
  surveys = await api("/api/projects/" + projectId + "/leak-surveys");
  const sel = $("#lkSelect");
  sel.innerHTML = "";
  if (!surveys.length) {
    const o = document.createElement("option");
    o.textContent = "(尚无校审,请在左栏新建)";
    sel.appendChild(o);
    L = null; renderAll();
    return;
  }
  for (const s of surveys) {
    const o = document.createElement("option");
    o.value = s.id;
    const st = s.stats;
    const tail = st ? " 超限 " + st.fail_length_m.toFixed(1) + "m" : "";
    o.textContent = "#" + s.id + " " + s.label +
      (s.status === "confirmed" ? " 已确认·修订" + s.revision : " 草稿·修订" + s.revision) + tail;
    sel.appendChild(o);
  }
  sel.value = selectId || surveys[0].id;
  await loadSurvey(+sel.value);
}

async function loadSurvey(lid) {
  L = await api("/api/leak-surveys/" + lid);
  if (!paths().some(p => p.id === selPathId)) selPathId = paths()[0]?.id ?? null;
  selS = null;
  renderAll();
}

/* ---------------------------------------------------------------- 总渲染 */

function renderAll() {
  renderHeader();
  renderRuns();
  renderParams();
  renderZonesPaths();
  renderOverrides();
  renderEvents();
  renderStats();
  renderRuns2();
  renderPairList();
  renderViewport();
  drawProfile();
  renderLegend();
}

function renderHeader() {
  const b = $("#lkBadge");
  if (!L) { b.textContent = ""; return; }
  b.textContent = (locked() ? "已确认 · " : "草稿 · ") + "修订 " + L.revision;
  b.classList.toggle("mixed", locked());
  $("#btnConfirm").classList.toggle("hidden", locked());
  $("#btnReopen").classList.toggle("hidden", !locked());
  for (const id of ["#btnExpSvg", "#btnExpCsv", "#btnExpJson"]) {
    $(id).disabled = !locked();
    $(id).title = locked() ? "导出(取自确认结果)" : "确认结果后才能导出(三份材料同源)";
  }
  for (const id of ["#btnImportOn", "#btnImportOff", "#btnSaveParams", "#btnOverride",
                    "#btnCreate"]) $(id).disabled = locked();
  $("#ownLoop").value = L.own_loop || "";
  $("#adjLoop").value = L.adjacent_loop || "";
}

/* ---------------------------------------------------------------- 平面图 */

function renderViewport() {
  viewport.innerHTML = "";
  if (!L) return;
  const b = L.project_bounds;
  const mx = b.width * 0.06, my = b.height * 0.06;
  viewport.setAttribute("viewBox", [b.min_x - mx, b.min_y - my,
    b.width + 2 * mx, b.height + 2 * my].join(" "));

  const gVenue = el("g", { opacity: 0.5 }, viewport);
  try {
    const doc = new DOMParser().parseFromString(L.venue_svg, "image/svg+xml");
    const root = doc.querySelector("svg");
    if (root) for (const n of [...root.childNodes]) gVenue.appendChild(document.importNode(n, true));
  } catch (e) { /* 底图解析失败不阻断 */ }

  el("g", { id: "zoneLayer" }, viewport);
  el("g", { id: "pathLayer" }, viewport);
  el("g", { id: "stationLayer" }, viewport);
  el("g", { id: "pointLayer" }, viewport);
  el("g", { id: "vertexLayer" }, viewport);
  el("g", { id: "draftLayer" }, viewport);

  renderZones();
  renderPaths();
  renderPoints();
  if (tool === "own" || tool === "adjacent" || tool === "boundary") drawDraft();
}

function renderZones() {
  const g = $("#zoneLayer");
  if (!g) return;
  for (const z of L.zones) {
    const pts = z.polygon.map(p => p[0] + "," + p[1]).join(" ");
    const poly = el("polygon", { points: pts, fill: ZONE_COLOR[z.kind],
      "fill-opacity": 0.05, stroke: ZONE_COLOR[z.kind], "stroke-width": 0.3,
      "stroke-dasharray": "1.4 0.9" }, g);
    el("title", {}, poly).textContent = ZONE_TEXT[z.kind] + " " + (z.name || "");
    const cx = z.polygon.reduce((a, p) => a + p[0], 0) / z.polygon.length;
    const cy = z.polygon.reduce((a, p) => a + p[1], 0) / z.polygon.length;
    const t = el("text", { x: cx, y: cy, "font-size": 2.2, fill: ZONE_COLOR[z.kind],
      "text-anchor": "middle", "pointer-events": "none" }, g);
    t.textContent = ZONE_TEXT[z.kind] + (z.name ? "·" + z.name : "");
  }
}

function renderPaths() {
  const g = $("#pathLayer");
  const gs = $("#stationLayer");
  const gv = $("#vertexLayer");
  if (!g) return;
  for (const p of paths()) {
    // 底层灰线
    el("polyline", { points: p.vertices.map(v => v.join(",")).join(" "),
      fill: "none", stroke: "#3a4350", "stroke-width": 1.0 }, g);
    // 按测站状态着色
    let prev = null;
    for (const st of p.stations) {
      if (prev) {
        const color = ST_COLOR[st.status] || "#888";
        const dash = st.status === "noconclusion" ? "0.7 0.5"
          : st.status === "nodata" ? "0.3 0.5" : "1 0";
        const ln = el("line", { x1: prev.x, y1: prev.y, x2: st.x, y2: st.y,
          stroke: color, "stroke-width": 0.8, "stroke-dasharray": dash,
          "stroke-linecap": "round", cursor: "pointer" }, gs);
        ln.addEventListener("click", (ev) => { ev.stopPropagation(); selectStation(p.id, st.s); });
        el("title", {}, ln).textContent =
          (p.name || "边界") + " " + st.s.toFixed(1) + "m " + ST_TEXT[st.status] +
          (st.excess != null ? " 外逸 " + st.excess + " dB" : "") +
          (st.reasons.length ? "\n" + st.reasons.map(ncReason).join(";") : "");
      }
      prev = st;
    }
    // 连续超限段:红色半透明带 + 峰值菱形
    for (const run of p.runs) {
      el("circle", { cx: run.peak_x, cy: run.peak_y, r: 1.25, fill: "#d24040",
        stroke: "#fff", "stroke-width": 0.25, cursor: "pointer" }, gs)
        .addEventListener("click", (ev) => { ev.stopPropagation(); selectStation(p.id, run.peak_s); });
    }
    // 选中测站高亮环
    if (selPathId === p.id && selS != null) {
      const st = p.stations.find(q => Math.abs(q.s - selS) < 0.51);
      if (st) el("circle", { cx: st.x, cy: st.y, r: 1.5, fill: "none",
        stroke: "#ffd75e", "stroke-width": 0.35 }, gs);
    }
    // 顶点句柄(选择工具/边界工具可拖可拆)
    p.vertices.forEach((v, i) => {
      const h = el("rect", { x: v[0] - 0.55, y: v[1] - 0.55, width: 1.1, height: 1.1,
        fill: locked() ? "#667" : "#cfd6e2", stroke: "#14171c", "stroke-width": 0.15,
        cursor: locked() ? "default" : "pointer" }, gv);
      h.addEventListener("mousedown", (ev) => onVertexDown(ev, p, i, h));
      el("title", {}, h).textContent = (p.name || "边界") + " 顶点 " + i +
        (locked() ? "(已锁定)" : " · 拖动调整;切到“画保密边界”工具单击可在此拆分");
    });
  }
}

function pairColor(p) {
  return p.status === "noconclusion" ? ST_COLOR.noconclusion : "#2e9e5b";
}

function renderPoints() {
  const g = $("#pointLayer");
  if (!g || !result()) return;
  const R = result();
  const showOn = viewMode !== "off", showOff = viewMode !== "on";
  for (const p of R.pairs) {
    const color = pairColor(p);
    if (viewMode === "both" && (Math.abs(p.x - p.rx) > 0.3 || Math.abs(p.y - p.ry) > 0.3))
      el("line", { x1: p.x, y1: p.y, x2: p.rx, y2: p.ry, stroke: color,
        "stroke-width": 0.15, "stroke-dasharray": "0.5 0.4" }, g);
    if (showOn) {
      const grp = el("g", { cursor: tool === "select" && !locked() ? "move" : "default" }, g);
      el("circle", { cx: p.x, cy: p.y, r: 0.62, fill: color,
        stroke: "rgba(255,255,255,.75)", "stroke-width": 0.15 }, grp);
      if (p.method === "manual") el("circle", { cx: p.x, cy: p.y, r: 1.0,
        fill: "none", stroke: "#ffd75e", "stroke-width": 0.22 }, grp);
      const t = el("title", {}, grp);
      t.textContent = "开 " + p.on_label + " ↔ 关 " + p.off_label +
        " (" + METHOD_TEXT[p.method] + ")" +
        (p.status === "noconclusion" ? "\n无结论:" + p.reasons.map(ncReason).join(";")
         : "\n外逸 " + (p.on_field - p.off_field).toFixed(1) + " dB") +
        (p.manual_note ? "\n改配:" + p.manual_note : "");
      grp.addEventListener("mousedown", (ev) => onPointDown(ev, p.on_label, "on", grp));
    }
    if (showOff) {
      const grp = el("g", { cursor: tool === "select" && !locked() ? "move" : "default" }, g);
      el("circle", { cx: p.rx, cy: p.ry, r: 0.5, fill: "none", stroke: color,
        "stroke-width": 0.2 }, grp);
      el("title", {}, grp).textContent = "关 " + p.off_label + " 背景 " + p.off_field + " dB";
      grp.addEventListener("mousedown", (ev) => onPointDown(ev, p.off_label, "off", grp));
    }
  }
  // 配对多解:紫方框
  for (const a of R.ambiguous) {
    if (a.x == null) continue;
    const isOn = a.side === "on";
    if ((isOn && !showOn) || (!isOn && !showOff)) continue;
    el("rect", { x: a.x - 0.5, y: a.y - 0.5, width: 1, height: 1, fill: "none",
      stroke: ST_COLOR.noconclusion, "stroke-width": 0.22 }, g);
    el("title", {}, g.lastChild).textContent =
      (isOn ? "开 " : "关 ") + a.label + " 配对多解,候选:" + a.candidates.join("/");
  }
  // 未配对:灰三角
  for (const u of (R.unpaired_points || [])) {
    const isOn = u.condition === "on";
    if ((isOn && !showOn) || (!isOn && !showOff)) continue;
    const s = 1.1, cx = u.x, cy = u.y;
    const tri = el("polygon", {
      points: cx + "," + (cy - s) + " " + (cx - s) + "," + (cy + s) + " " +
              (cx + s) + "," + (cy + s),
      fill: "none", stroke: "#8a8f98", "stroke-width": 0.22,
      cursor: tool === "select" && !locked() ? "move" : "default" }, g);
    el("title", {}, tri).textContent = (isOn ? "开 " : "关 ") + u.label + "(未配对)";
    tri.addEventListener("mousedown", (ev) => onPointDown(ev, u.label, u.condition, tri));
  }
}

function renderLegend() {
  const items = [["#2e9e5b", "合格"], ["#d24040", "超限外逸"], [ST_COLOR.noconclusion, "无结论"],
                 [ST_COLOR.nodata, "无数据"], ["#4da3ff", "本环服务区"],
                 ["#f0932b", "相邻环区"], ["#ffd75e", "峰值/选中"]];
  $("#legend").innerHTML = items.map(i =>
    "<div class='item'><span class='sw' style='background:" + i[0] +
    ";border-radius:0'></span>" + i[1] + "</div>").join("");
}

/* ---------------------------------------------------------------- 里程曲线 */

function selectStation(pid, s) {
  selPathId = pid; selS = s;
  renderPaths(); renderViewport();
  drawProfile(); renderRuns2();
  const p = selPath();
  if (p) {
    const st = p.stations.find(q => Math.abs(q.s - s) < 0.51);
    if (st) status((p.name || "边界") + " @ " + s.toFixed(1) + "m: " + ST_TEXT[st.status] +
      (st.excess != null ? " 外逸 " + st.excess + " dB / 开启 " + st.on_field +
       " dB / 背景 " + st.background + " dB" : "") +
      (st.reasons.length ? " — " + st.reasons.map(ncReason).join(";") : ""));
  }
}

function drawProfile() {
  profile.innerHTML = "";
  const W = 360, H = 220, Lm = 40, Rm = 10, T = 12, B = 26;
  const p = selPath();
  $("#profileTitle").textContent = p ? (p.name || "边界 #" + p.id) : "";
  const params = L ? L.params : { leak_limit_db: 6, max_field_db: -32 };

  if (!p) {
    const t = el("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, profile);
    t.textContent = "导入两种工况、画出保密边界后显示里程曲线";
    return;
  }
  const sts = p.stations;
  const len = p.length_m || 1;
  const xs = (s) => Lm + s / len * (W - Lm - Rm);
  const vals = sts.flatMap(t => [t.excess, t.on_field, t.background].filter(v => v != null));
  let lo = Math.min(-5, ...vals) - 3;
  let hi = Math.max(params.leak_limit_db + 4, ...vals) + 3;
  const ys = (v) => T + (hi - v) / (hi - lo) * (H - T - B);

  for (let v = Math.ceil(lo / 5) * 5; v <= hi; v += 5) {
    el("line", { x1: Lm, y1: ys(v), x2: W - Rm, y2: ys(v), class: "grid" }, profile);
    const t = el("text", { x: 4, y: ys(v) + 3 }, profile); t.textContent = v;
  }
  el("line", { x1: Lm, y1: H - B, x2: W - Rm, y2: H - B, class: "axis" }, profile);
  el("line", { x1: Lm, y1: T, x2: Lm, y2: H - B, class: "axis" }, profile);
  for (let s = 0; s <= len + 0.01; s += Math.max(1, Math.round(len / 8))) {
    const t = el("text", { x: xs(s), y: H - B + 12, "text-anchor": "middle" }, profile);
    t.textContent = s + "m";
  }
  // 限值线(外逸量)
  el("line", { x1: Lm, y1: ys(params.leak_limit_db), x2: W - Rm,
    y2: ys(params.leak_limit_db), class: "limit-line" }, profile);
  const lt = el("text", { x: W - Rm, y: ys(params.leak_limit_db) - 2,
    "text-anchor": "end" }, profile);
  lt.textContent = "外逸限值 " + params.leak_limit_db + " dB";

  const drawLine = (key, cls) => {
    const pts2 = sts.filter(t => t[key] != null && (t.status === "ok" || t.status === "fail"))
      .map(t => xs(t.s) + "," + ys(t[key]));
    if (pts2.length > 1) el("polyline", { points: pts2.join(" "), class: cls }, profile);
  };
  drawLine("background", "noise-line");
  drawLine("on_field", "retest-line");
  drawLine("excess", "field-line");

  // 状态色点 + 无结论段紫色竖条
  for (const st of sts) {
    if (st.excess != null) {
      el("circle", { cx: xs(st.s), cy: ys(st.excess), r: 2.6,
        fill: st.status === "fail" ? "#d24040"
          : st.status === "noconclusion" ? ST_COLOR.noconclusion : "#4dd07a",
        stroke: "#0e1116", "stroke-width": 0.5, cursor: "pointer" }, profile)
        .addEventListener("click", () => selectStation(p.id, st.s));
    } else {
      el("rect", { x: xs(st.s) - 1.2, y: T, width: 2.4, height: H - T - B,
        fill: ST_COLOR[st.status] || "#888", opacity: 0.25, cursor: "pointer" }, profile)
        .addEventListener("click", () => selectStation(p.id, st.s));
    }
  }
  // 峰值标记
  for (const run of p.runs) {
    const t = el("text", { x: xs(run.peak_s), y: ys(run.peak_excess) - 6,
      "text-anchor": "middle", class: "delta", cursor: "pointer" }, profile);
    t.textContent = "▲" + run.peak_excess;
    t.addEventListener("click", () => selectStation(p.id, run.peak_s));
  }
  if (selS != null)
    el("line", { x1: xs(selS), y1: T, x2: xs(selS), y2: H - B,
      stroke: "#ffd75e", "stroke-width": 0.8, "stroke-dasharray": "2 2" }, profile);
  const cap = el("text", { x: W - Rm, y: H - 2, "text-anchor": "end" }, profile);
  cap.textContent = "里程 m / dB 绿:外逸 橙虚:开启场强 灰虚:关闭背景";
}

/* ---------------------------------------------------------------- 侧栏 */

function renderParams() {
  if (!L) return;
  document.querySelectorAll("#paramsForm input").forEach(inp => {
    inp.value = L.params[inp.dataset.k];
    inp.disabled = locked();
  });
}

function renderRuns() {
  const ul = $("#runList");
  ul.innerHTML = "";
  if (!L) return;
  if (!L.runs.length) ul.innerHTML = "<li class='dim'>尚未导入测次</li>";
  for (const r of L.runs) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='dot' style='background:" +
      (r.condition === "on" ? "#f0932b" : "#8a8f98") + "'></span>" +
      (r.condition === "on" ? "开启" : "关闭") + " " + esc(r.label) +
      "<span class='dim'> " + r.n_points + " 点</span>" +
      (r.time_text ? "<span class='dim'> " + esc(r.time_text) + "</span>" : "") +
      (locked() ? "" : "<span class='del' title='删除测次'>✕</span>");
    const del = li.querySelector(".del");
    if (del) del.onclick = async () => {
      L = await api("/api/leak-surveys/" + L.id + "/runs/" + r.id, { method: "DELETE" });
      status("测次已删除,沿线结果已重算"); renderAll();
    };
    ul.appendChild(li);
  }
  const gap = result()?.run_time_gap_h;
  $("#importMsg").textContent = gap != null
    ? "⚠ 两测次相隔 " + gap + " h,超出时间窗:全线时段不重叠,保持无结论"
    : "";
}

function renderZonesPaths() {
  const ul = $("#pathList");
  ul.innerHTML = "";
  if (!L) return;
  for (const z of L.zones) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='dot' style='background:" + ZONE_COLOR[z.kind] + "'></span>" +
      ZONE_TEXT[z.kind] + " " + esc(z.name || "") +
      (locked() ? "" : "<span class='del' title='删除环区'>✕</span>");
    li.querySelector(".del")?.addEventListener("click", async () => {
      L = await api("/api/leak-zones/" + z.id, { method: "DELETE" });
      status("环区已删除,已重算"); renderAll();
    });
    ul.appendChild(li);
  }
  for (const p of paths()) {
    const li = document.createElement("li");
    if (p.id === selPathId) li.classList.add("sel");
    const peak = p.peak ? "峰 " + p.peak.excess + "dB" : "";
    li.innerHTML = "<span class='dot' style='background:#d24040'></span>" +
      (esc(p.name || "边界 #" + p.id)) + " " + p.length_m.toFixed(1) + "m " +
      (p.self_intersect ? "<span class='tag' style='color:#c79af0'>自交</span>" : "") +
      "<span class='dim'> 超限 " + p.stats.fail_m.toFixed(1) + "m / 无结论 " +
      p.stats.noconclusion_m.toFixed(1) + "m " + peak + "</span>" +
      (locked() ? "" : "<span class='act' title='在内部顶点处拆分(需备注)'>拆</span>" +
                       "<span class='del' title='删除边界'>✕</span>");
    li.onclick = () => { selPathId = p.id; selS = null; renderZonesPaths(); renderPaths(); drawProfile(); };
    li.querySelector(".del")?.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      L = await api("/api/leak-paths/" + p.id, { method: "DELETE" });
      status("边界已删除,已重算"); renderAll();
    });
    li.querySelector(".act")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const k = prompt("在第几个内部顶点处拆分?(0.." + (p.vertices.length - 1) +
        ",不含两端)\n拆分后将在该点分成两段。", "1");
      if (k == null) return;
      doPathAction(p.id, { action: "split", at_vertex: +k });
    });
    ul.appendChild(li);
  }
  if (!L.zones.length && !paths().length)
    ul.innerHTML = "<li class='dim'>用上方工具圈环区、画保密边界</li>";
}

function renderOverrides() {
  const ul = $("#overrideList");
  ul.innerHTML = "";
  if (!L) return;
  if (!L.overrides.length) ul.innerHTML = "<li class='dim'>暂无人工改配</li>";
  for (const o of L.overrides) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='tag'>改配</span>" + esc(o.on_label) + " → " +
      (o.off_label ? esc(o.off_label) : "(取消配对)") +
      "<span class='dim'> " + esc(o.note) + "</span>" +
      (locked() ? "" : "<span class='del' title='撤销改配'>✕</span>");
    li.querySelector(".del")?.addEventListener("click", async () => {
      let note = $("#editNote").value.trim();
      if (!note) note = prompt("撤销该改配的备注理由(将形成新修订):");
      if (!note) { status("已取消:撤销改配必须备注"); return; }
      L = await api("/api/leak-surveys/" + L.id + "/overrides/" + o.id, {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      status("改配已撤销,已切换至修订 " + L.revision); renderAll();
    });
    ul.appendChild(li);
  }
}

function renderEvents() {
  const ul = $("#eventList");
  ul.innerHTML = "";
  if (!L) return;
  if (!L.events.length) ul.innerHTML = "<li class='dim'>暂无事件</li>";
  for (const e of L.events) {
    const li = document.createElement("li");
    const p = e.payload;
    let txt = EVENT_TEXT[e.kind] || e.kind;
    if (e.kind === "import-run") txt += " " + (p.condition === "on" ? "开启" : "关闭") + " " + p.rows + " 点";
    else if (e.kind === "move") txt += " " + (p.condition === "on" ? "开" : "关") + " " + p.point_label;
    else if (e.kind === "override") txt += " " + p.on_label + "→" + (p.off_label || "取消");
    else if (e.kind === "path-split") txt += " 顶点 " + p.split_at_vertex;
    li.innerHTML = "<span class='tag'>修订" + e.revision + "</span>" + esc(txt) +
      "<span class='dim' style='margin-left:auto'>" + e.created_at.slice(5, 16) + "</span>";
    li.title = p.note || JSON.stringify(p);
    ul.appendChild(li);
  }
}

function renderStats() {
  const box = $("#stats");
  if (!L || !result()) { box.innerHTML = "<span class='dim'>尚未计算</span>"; return; }
  const s = result().stats;
  const gap = result().run_time_gap_h;
  const chips = [
    ["#2e9e5b", "配对 " + s.n_pairs],
    ["#d24040", "超限 " + s.fail_length_m.toFixed(1) + " m"],
    [ST_COLOR.noconclusion, "无结论 " + s.noconclusion_length_m.toFixed(1) + " m"],
    ["#8a8f98", "多解 " + s.n_ambiguous],
    ["#8a8f98", "仅开 " + s.n_on_only + " 仅关 " + s.n_off_only],
  ];
  if (s.peak) chips.push(["#ffd75e", "峰值 " + s.peak.excess + " dB@" + s.peak.s.toFixed(1) + "m"]);
  if (s.min_adj_margin != null) chips.push(["#f0932b", "相邻环余量 " + s.min_adj_margin.toFixed(1) + " dB"]);
  box.innerHTML = "<div class='chips'>" + chips.map(c =>
    "<span class='chip' style='background:" + c[0] + "'>" + c[1] + "</span>").join("") + "</div>" +
    (gap != null ? "<p class='issue'>两种工况时段不重叠(相隔 " + gap + " h)</p>" : "");
}

function renderRuns2() {
  const ul = $("#runList2");
  ul.innerHTML = "";
  if (!L) return;
  for (const p of paths()) {
    if (!p.runs.length) continue;
    for (const run of p.runs) {
      const li = document.createElement("li");
      const active = selPathId === p.id && selS != null &&
        Math.abs(selS - run.peak_s) < 0.51;
      if (active) li.classList.add("sel");
      li.innerHTML = "<span class='dot' style='background:#d24040'></span>" +
        (esc(p.name || "边界 #" + p.id)) + " " + run.s0.toFixed(1) + "–" + run.s1.toFixed(1) +
        "m(连续 " + run.length_m.toFixed(1) + "m)<span class='dim'>峰 " +
        run.peak_excess + " dB" +
        (run.min_adj_margin != null ? " · 相邻环余量 " + run.min_adj_margin + " dB" : "") +
        "</span>";
      li.onclick = () => selectStation(p.id, run.peak_s);
      ul.appendChild(li);
    }
  }
  if (!ul.children.length)
    ul.innerHTML = "<li class='dim'>无连续超限段</li>";
}

function renderPairList() {
  const ul = $("#pairList");
  ul.innerHTML = "";
  if (!L || !result()) return;
  const R = result();
  $("#pairCount").textContent = "(" + R.pairs.length + ")";
  for (const p of R.pairs) {
    const li = document.createElement("li");
    const nc = p.status === "noconclusion";
    li.innerHTML = "<span class='dot' style='background:" + pairColor(p) + "'></span>" +
      esc(p.on_label) + "↔" + esc(p.off_label) +
      (p.method === "manual" ? "<span class='tag'>人工</span>" : "") +
      "<span class='dim' style='margin-left:auto'>" +
      (nc ? p.reasons.map(ncReason).join("/") : (p.on_field - p.off_field).toFixed(1) + " dB") +
      "</span>";
    li.title = nc ? p.reasons.map(ncReason).join(";") : "外逸量 " + (p.on_field - p.off_field) + " dB";
    li.onclick = () => { selPathId = selPath()?.id ?? null; };
    ul.appendChild(li);
  }
  for (const a of R.ambiguous) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='dot' style='background:" + ST_COLOR.noconclusion + "'></span>" +
      esc(a.label) + "<span class='tag'>多解</span><span class='dim' style='margin-left:auto'>" +
      esc(a.candidates.join("/")) + "</span>";
    ul.appendChild(li);
  }
}

/* ---------------------------------------------------------------- 交互:绘图 */

document.querySelectorAll(".tool").forEach(b => b.onclick = () => {
  tool = b.dataset.tool;
  document.querySelectorAll(".tool").forEach(x => x.classList.toggle("active", x === b));
  draft = [];
  const drawing = tool === "own" || tool === "adjacent" || tool === "boundary";
  $("#drawBar").classList.toggle("hidden", !drawing);
  viewport.classList.toggle("drawing", drawing);
  const g = $("#draftLayer"); if (g) g.innerHTML = "";
  status(tool === "boundary" ? "单击加边界顶点,双击或“完成”结束;单击已有顶点可拆分"
    : drawing ? "单击添加顶点,双击或“完成”闭合"
    : tool === "pan" ? "拖动平移视图" : "点击选择;可拖动误定位测点或边界顶点");
  renderViewport();
});

$("#btnDrawDone").onclick = finishDraw;
$("#btnDrawCancel").onclick = () => {
  draft = []; $("#drawBar").classList.add("hidden");
  const g = $("#draftLayer"); if (g) g.innerHTML = "";
};

viewport.addEventListener("click", (ev) => {
  if (tool === "own" || tool === "adjacent") {
    const p = svgPoint(ev.clientX, ev.clientY);
    draft.push([+p.x.toFixed(2), +p.y.toFixed(2)]);
    drawDraft();
  } else if (tool === "boundary") {
    if (ev.target === viewport || ev.target.parentNode === $("#stationLayer") ||
        ev.target.parentNode === $("#pathLayer")) {
      const p = svgPoint(ev.clientX, ev.clientY);
      draft.push([+p.x.toFixed(2), +p.y.toFixed(2)]);
      drawDraft();
    }
  } else if (tool === "select" && ev.target === viewport) {
    selS = null; renderPaths(); drawProfile();
  }
});

viewport.addEventListener("dblclick", () => {
  if ((tool === "own" || tool === "adjacent") && draft.length >= 3) finishDraw();
  else if (tool === "boundary" && draft.length >= 2) finishDraw();
});

function drawDraft() {
  const g = $("#draftLayer");
  g.innerHTML = "";
  const color = tool === "boundary" ? "#d24040" : ZONE_COLOR[tool] || "#4da3ff";
  if (draft.length > 1)
    el("polyline", { points: draft.map(p => p.join(",")).join(" "), fill: "none",
      stroke: color, "stroke-width": 0.3, "stroke-dasharray": "1 0.6" }, g);
  for (const p of draft) el("circle", { cx: p[0], cy: p[1], r: 0.4, fill: color }, g);
}

async function finishDraw() {
  const name = $("#drawName").value || "";
  if (tool === "boundary") {
    if (draft.length < 2) { status("保密边界至少 2 个顶点"); return; }
    L = await api("/api/leak-surveys/" + L.id + "/paths", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, vertices: draft }),
    });
    status("已添加保密边界,沿线结果已重算");
    selPathId = L.result.paths[L.result.paths.length - 1].id;
  } else {
    if (draft.length < 3) { status("环区至少 3 个顶点"); return; }
    L = await api("/api/leak-surveys/" + L.id + "/zones", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: tool, name, polygon: draft }),
    });
    status("已添加" + ZONE_TEXT[tool] + ",沿线结果已重算");
  }
  draft = [];
  $("#drawBar").classList.add("hidden");
  $("#drawName").value = "";
  renderAll();
}

/* ---------------------------------------------------------------- 拖动测点 / 顶点 */

function requireNote() {
  const note = $("#editNote").value.trim();
  if (!note) { status("人工改配/调整边界/移动测点必须先在左栏填写备注理由"); return null; }
  return note;
}

function onPointDown(ev, label, condition, grp) {
  if (tool !== "select" || locked()) return;
  ev.preventDefault(); ev.stopPropagation();
  drag = { kind: "point", label, condition, grp, moved: false };
}

function onVertexDown(ev, pathRow, i, handle) {
  if (locked()) return;
  ev.preventDefault(); ev.stopPropagation();
  if (tool === "boundary") {
    // 边界工具下单击顶点 = 拆分
    doPathAction(pathRow.id, { action: "split", at_vertex: i });
    return;
  }
  if (tool !== "select") return;
  drag = { kind: "vertex", pathId: pathRow.id, index: i, handle, moved: false };
}

window.addEventListener("mousemove", (ev) => {
  if (!drag) return;
  const p = svgPoint(ev.clientX, ev.clientY);
  drag.x = +p.x.toFixed(2); drag.y = +p.y.toFixed(2);
  drag.moved = true;
  if (drag.kind === "point") {
    const c = drag.grp.querySelector("circle");
    c.setAttribute("cx", drag.x); c.setAttribute("cy", drag.y);
  } else if (drag.kind === "vertex") {
    drag.handle.setAttribute("x", drag.x - 0.55);
    drag.handle.setAttribute("y", drag.y - 0.55);
  }
});

window.addEventListener("mouseup", async () => {
  if (!drag) return;
  const d = drag; drag = null;
  if (!d.moved) return;
  try {
    if (d.kind === "point") {
      const note = requireNote();
      if (!note) { renderViewport(); return; }
      L = await api("/api/leak-surveys/" + L.id + "/move", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ point_label: d.label, condition: d.condition,
          x: d.x, y: d.y, note }),
      });
      status("测点 " + d.label + " 已移动,已切换至修订 " + L.revision);
    } else if (d.kind === "vertex") {
      const note = requireNote();
      if (!note) { renderViewport(); return; }
      const p = paths().find(q => q.id === d.pathId);
      const verts = p.vertices.map(v => v.slice());
      verts[d.index] = [d.x, d.y];
      await doPathAction(d.pathId, { action: "vertices", vertices: verts, note });
    }
  } catch (e) { /* 已 status 提示 */ }
  renderAll();
});

async function doPathAction(pid, body) {
  if (body.action !== "vertices") {
    const note = requireNote();
    if (!note) return L;
    body.note = note;
  }
  L = await api("/api/leak-paths/" + pid + "/vertices", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  status(body.action === "split"
    ? "边界已拆分为两段,已切换至修订 " + L.revision
    : "边界顶点已更新,已切换至修订 " + L.revision);
  renderAll();
  return L;
}

/* ---------------------------------------------------------------- 平移缩放 */

viewport.addEventListener("mousedown", (ev) => {
  if (tool === "pan" && ev.target === viewport) {
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

document.querySelectorAll("#viewMode button").forEach(b => b.onclick = () => {
  viewMode = b.dataset.mode;
  document.querySelectorAll("#viewMode button").forEach(x => x.classList.toggle("active", x === b));
  renderPoints();
});

/* ---------------------------------------------------------------- 操作 */

$("#btnCreate").onclick = async () => {
  const st = await api("/api/projects/" + projectId + "/leak-surveys", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label: $("#lkLabel").value, own_loop: $("#ownLoop").value,
      adjacent_loop: $("#adjLoop").value }),
  });
  status("已新建边界外逸校审 #" + st.id);
  $("#lkLabel").value = "";
  await refreshSurveyList(st.id);
};

$("#lkSelect").onchange = (ev) => loadSurvey(+ev.target.value);

$("#btnImportOn").onclick = () => { pendingImport = "on"; $("#runFile").click(); };
$("#btnImportOff").onclick = () => { pendingImport = "off"; $("#runFile").click(); };
$("#runFile").onchange = async () => {
  const f = $("#runFile").files[0];
  if (!f || !pendingImport) return;
  const fd = new FormData();
  fd.append("csv", f);
  fd.append("condition", pendingImport);
  if ($("#runLabel").value) fd.append("label", $("#runLabel").value);
  if ($("#runTime").value) fd.append("time_text", $("#runTime").value);
  L = await api("/api/leak-surveys/" + L.id + "/runs", { method: "POST", body: fd });
  const r = L.import_result;
  status((pendingImport === "on" ? "开启" : "关闭") + "工况已导入 " + r.rows + " 点,沿线已重算" +
    (r.csv_errors.length ? ";CSV 警告 " + r.csv_errors.length + " 条" : ""));
  pendingImport = null;
  $("#runFile").value = ""; $("#runLabel").value = ""; $("#runTime").value = "";
  renderAll();
};

$("#btnSaveParams").onclick = async () => {
  const data = {};
  document.querySelectorAll("#paramsForm input").forEach(inp => data[inp.dataset.k] = inp.value);
  L = await api("/api/leak-surveys/" + L.id + "/params", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  status("判定参数已保存,沿线结果已重算");
  renderAll();
};

$("#btnOverride").onclick = async () => {
  const note = requireNote();
  if (!note) return;
  L = await api("/api/leak-surveys/" + L.id + "/overrides", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ on_label: $("#ovOn").value.trim(),
      off_label: $("#ovOff").value.trim(), note }),
  });
  $("#ovOn").value = $("#ovOff").value = "";
  status("人工改配已记录,已切换至修订 " + L.revision);
  renderAll();
};

$("#btnConfirm").onclick = async () => {
  try {
    L = await api("/api/leak-surveys/" + L.id + "/confirm", { method: "POST" });
    status("校审已确认:来源测次、配对表、限值与边界已锁定,可导出三份材料");
    renderAll(); refreshSurveyList(L.id);
  } catch (e) { /* 已提示 */ }
};

$("#btnReopen").onclick = async () => {
  const note = prompt("重审原因(将形成新修订号):", "来源修订后重审");
  if (note == null) return;
  L = await api("/api/leak-surveys/" + L.id + "/reopen", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
  status("已解除锁定,修订号 +1;调整后请重新确认");
  renderAll(); refreshSurveyList(L.id);
};

$("#btnExpSvg").onclick = () => L && (location = "/api/leak-surveys/" + L.id + "/export/boundary.svg");
$("#btnExpCsv").onclick = () => L && (location = "/api/leak-surveys/" + L.id + "/export/remeasure.csv");
$("#btnExpJson").onclick = () => L && (location = "/api/leak-surveys/" + L.id + "/export/recalc.json");

boot();
