/* 驱动基准工作区 — 前端(原生 JS + SVG)
 * 座位图:归一化覆盖网格 + 测点(原始读数与归一化值并列);
 * 时间曲线:环路电流/告警/锚点/测点映射位置,与座位图联动核对绑定;
 * 换绑锚点、保留异常段必须备注理由并形成新修订;确认后锁定,导出同源。
 */
"use strict";

const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

const CELL_COLOR = { ok: "#2e9e5b", warn: "#d8a012", fail: "#d24040",
                     nodata: "#8a8f98", noconclusion: "#9b59b6", notest: "#3a3f47" };
const CELL_TEXT = { ok: "合格", warn: "临近干扰", fail: "不合格",
                    nodata: "证据不足", noconclusion: "不作结论", notest: "禁测区" };
const PT_COLOR = { ok: "#1c6dd9", excluded: "#9b59b6" };
const KEEP_TEXT = { clip: "削波", overheat: "过热", "sample-gap": "采样断档" };
const EVENT_TEXT = {
  create: "创建校审", "import-record": "导入功放记录", "delete-record": "删除功放记录",
  "import-points": "导入场强记录", anchor: "绑定锚点", "anchor-delete": "删除锚点",
  keep: "保留异常段", "keep-delete": "撤销保留段", params: "调整参数",
  confirm: "确认锁定", reopen: "重审新修订", "compare-ref": "被对照引用",
};

let projectId = null;
let surveys = [];
let D = null;               // 当前校审状态
let compares = [];
let selPoint = null;        // 选中测点 label
let pan = null;
let snapView = null;        // 正在查看的历史修订快照(null = 当前修订)

const viewport = $("#viewport");
const chart = $("#currentChart");

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

function fmtClock(sec) {
  if (sec == null) return "—";
  if (Math.abs(sec) >= 946684800) {           // epoch(ISO 日期时间解析而来)
    const d = new Date(sec * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return (d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
           p(d.getHours()) + ":" + p(d.getMinutes());
  }
  const neg = sec < 0, a = Math.abs(sec);
  const h = Math.floor(a / 3600), m = Math.floor(a % 3600 / 60), s = a % 60;
  const ss = (s % 1 ? s.toFixed(1) : String(s)).padStart(2, "0");
  return (neg ? "-" : "") + (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + ss;
}

function ncReason(code) {
  const [k, arg] = code.split(":");
  const t = (D && D.nc_text[k]) || k;
  return arg ? t + "(" + arg + ")" : t;
}

/* 快照查看时,结果相关渲染一律取快照数据(锚点/保留段/测点状态均为当时值) */
function effResult() { return snapView ? snapView.result : (D && D.result); }
function effSamples() { return snapView ? snapView.samples : (D ? D.samples : []); }
function effAnchors() { return snapView ? snapView.result.anchors : (D ? D.anchors : []); }
function effKeeps() { return snapView ? snapView.result.keeps : (D ? D.keeps : []); }
function effRecord() { return snapView ? snapView.record : (D ? D.record : null); }
function result() { return D && D.result; }
function points() { const r = effResult(); return r ? r.points : []; }
function locked() { return (D && D.status === "confirmed") || !!snapView; }
function fmtDb(v) { return v == null ? "—" : (+v).toFixed(1); }

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
  surveys = await api("/api/projects/" + projectId + "/drive-surveys");
  const sel = $("#drvSelect");
  sel.innerHTML = "";
  if (!surveys.length) {
    const o = document.createElement("option");
    o.textContent = "(尚无校审,请在左栏新建)";
    sel.appendChild(o);
    D = null; renderAll();
    return;
  }
  for (const s of surveys) {
    const o = document.createElement("option");
    o.value = s.id;
    const st = s.stats;
    const tail = st ? " 合格 " + st.n_ok + "/排除 " + st.n_excluded : "";
    o.textContent = "#" + s.id + " " + s.label +
      (s.status === "confirmed" ? " 已确认·修订" + s.revision : " 草稿·修订" + s.revision) + tail;
    sel.appendChild(o);
  }
  sel.value = selectId || surveys[0].id;
  await loadSurvey(+sel.value);
  await refreshCompares();
}

async function loadSurvey(did) {
  D = await api("/api/drive-surveys/" + did);
  snapView = null;
  selPoint = null;
  renderAll();
}

async function refreshCompares() {
  compares = await api("/api/projects/" + projectId + "/drive-compares");
  renderComparePanel();
}

/* ---------------------------------------------------------------- 总渲染 */

function renderAll() {
  renderHeader();
  renderRecord();
  renderAnchors();
  renderKeeps();
  renderParams();
  renderEvents();
  renderSnapList();
  renderStats();
  renderPointList();
  renderDetail();
  renderViewport();
  drawChart();
  renderLegend();
  renderComparePanel();
}

function renderHeader() {
  const b = $("#drvBadge");
  if (!D) { b.textContent = ""; return; }
  b.textContent = (D.status === "confirmed" ? "已确认 · " : "草稿 · ") +
    "修订 " + D.revision + (snapView ? "(查看历史 " + snapView.revision + ")" : "");
  b.classList.toggle("mixed", D.status === "confirmed" && !snapView);
  $("#btnConfirm").classList.toggle("hidden", D.status === "confirmed" || !!snapView);
  $("#btnReopen").classList.toggle("hidden", D.status !== "confirmed" || !!snapView);
  $("#snapBar").classList.toggle("hidden", !snapView);
  if (snapView) $("#snapRev").textContent = "修订 " + snapView.revision;
  const canExport = snapView || D.status === "confirmed";
  for (const id of ["#btnExpSvg", "#btnExpCsv", "#btnExpJson"]) {
    $(id).disabled = !canExport;
    $(id).title = snapView ? "导出当前查看的历史修订快照"
      : D.status === "confirmed" ? "导出(取自确认结果)"
      : "确认结果后才能导出(三份材料同源)";
  }
  for (const id of ["#btnImportRec", "#btnImportPts", "#btnAnchor", "#btnKeep",
                    "#btnSaveParams", "#btnCreate"]) $(id).disabled = locked();
}

/* ---------------------------------------------------------------- 座位图 */

function renderViewport() {
  viewport.innerHTML = "";
  if (!D) return;
  const b = D.project_bounds;
  const mx = b.width * 0.06, my = b.height * 0.06;
  viewport.setAttribute("viewBox", [b.min_x - mx, b.min_y - my,
    b.width + 2 * mx, b.height + 2 * my].join(" "));

  const gVenue = el("g", { opacity: 0.5 }, viewport);
  try {
    const doc = new DOMParser().parseFromString(D.venue_svg, "image/svg+xml");
    const root = doc.querySelector("svg");
    if (root) for (const n of [...root.childNodes]) gVenue.appendChild(document.importNode(n, true));
  } catch (e) { /* 底图解析失败不阻断 */ }

  const r = effResult();
  if (r) {
    const gCells = el("g", {}, viewport);
    const cs = r.grid.cs;
    for (const c of r.cells) {
      const rect = el("rect", { x: c.x - cs / 2, y: c.y - cs / 2, width: cs, height: cs,
        fill: CELL_COLOR[c.status] || "#888", "fill-opacity": 0.5,
        stroke: "#14171c", "stroke-width": 0.06 }, gCells);
      el("title", {}, rect).textContent =
        CELL_TEXT[c.status] + (c.field != null ? " " + c.field + " dB" : "") +
        (c.reason ? "\n" + c.reason : "");
    }
  }
  const gPts = el("g", {}, viewport);
  for (const p of points()) {
    const color = PT_COLOR[p.status] || "#888";
    const grp = el("g", { cursor: "pointer" }, gPts);
    el("circle", { cx: p.x, cy: p.y, r: 0.6, fill: color,
      stroke: "rgba(255,255,255,.8)", "stroke-width": 0.15 }, grp);
    if (p.kept.length) el("circle", { cx: p.x, cy: p.y, r: 1.0, fill: "none",
      stroke: "#ffd75e", "stroke-width": 0.25 }, grp);
    if (p.label === selPoint) el("circle", { cx: p.x, cy: p.y, r: 1.35, fill: "none",
      stroke: "#ffd75e", "stroke-width": 0.4 }, grp);
    el("title", {}, grp).textContent = p.label +
      " 原始 " + fmtDb(p.raw_ref_db) + " → 归一 " + fmtDb(p.norm_ref_db) + " dB" +
      (p.reasons.length ? "\n排除:" + p.reasons.map(ncReason).join(";") : "") +
      (p.kept.length ? "\n保留段豁免:" + p.kept.map(ncReason).join(";") : "");
    grp.addEventListener("click", (ev) => { ev.stopPropagation(); selectPoint(p.label); });
  }
}

function renderLegend() {
  const items = [["#1c6dd9", "进入覆盖(归一化)"], ["#9b59b6", "被排除测点"],
                 ["#ffd75e", "保留段豁免/选中"], ["#2e9e5b", "覆盖合格"],
                 ["#d24040", "覆盖不合格"], ["#8a8f98", "证据不足"]];
  $("#legend").innerHTML = items.map(i =>
    "<div class='item'><span class='sw' style='background:" + i[0] + "'></span>" +
    i[1] + "</div>").join("");
}

/* ---------------------------------------------------------------- 电流时间曲线 */

function chartRange() {
  const ts = [], cs = [];
  const rec = effRecord();
  if (rec && rec.t0 != null) ts.push(rec.t0, rec.t1);
  for (const a of effAnchors()) ts.push(a.amp_t);
  for (const p of points()) if (p.amp_t != null) ts.push(p.amp_t);
  for (const s of effSamples()) cs.push(s.current);
  if (D) cs.push(D.params.ref_current_a, D.params.calib_min_a, D.params.calib_max_a);
  for (const p of points()) if (p.current_a != null) cs.push(p.current_a);
  const t0 = ts.length ? Math.min(...ts) : 0, t1 = ts.length ? Math.max(...ts) : 100;
  const c1 = cs.length ? Math.max(...cs) * 1.15 : 5;
  return [t0, t1 === t0 ? t0 + 1 : t1, 0, c1];
}

function drawChart() {
  chart.innerHTML = "";
  const W = 380, H = 240, Lm = 42, Rm = 8, T = 10, B = 30;
  const rec = effRecord();
  $("#chartTitle").textContent = rec ? esc(rec.label) : "";
  const samples = effSamples();
  if (!D || !samples.length) {
    const t = el("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, chart);
    t.textContent = "导入功放记录与锚点后显示电流曲线与测点映射";
    return;
  }
  const [t0, t1, c0, c1] = chartRange();
  const P = effResult() ? effResult().params : D.params;
  const xs = (t) => Lm + (t - t0) / (t1 - t0) * (W - Lm - Rm);
  const ys = (c) => T + (c1 - c) / (c1 - c0) * (H - T - B);

  // 校准量程带 + 参考电流线
  el("rect", { x: Lm, y: ys(Math.min(P.calib_max_a, c1)), width: W - Lm - Rm,
    height: Math.max(1, ys(P.calib_min_a) - ys(Math.min(P.calib_max_a, c1))),
    fill: "rgba(46,158,91,.10)" }, chart);
  // 采样断档红带
  for (const g of (effResult()?.samples_summary.gaps || []))
    el("rect", { x: xs(g.t0), y: T, width: Math.max(1.5, xs(g.t1) - xs(g.t0)),
      height: H - T - B, fill: "rgba(210,64,64,.14)" }, chart)
      .appendChild(document.createElementNS(SVGNS, "title"))
      .textContent = "采样断档 " + g.gap_s + " s";

  // 网格与坐标
  const cStep = Math.pow(10, Math.floor(Math.log10(c1 / 4))) || 1;
  for (let c = 0; c <= c1; c += cStep) {
    el("line", { x1: Lm, y1: ys(c), x2: W - Rm, y2: ys(c), class: "grid" }, chart);
    const t = el("text", { x: 4, y: ys(c) + 3 }, chart);
    t.textContent = +c.toFixed(6);
  }
  el("line", { x1: Lm, y1: H - B, x2: W - Rm, y2: H - B, class: "axis" }, chart);
  el("line", { x1: Lm, y1: T, x2: Lm, y2: H - B, class: "axis" }, chart);
  const nTicks = 6;
  for (let i = 0; i <= nTicks; i++) {
    const tv = t0 + (t1 - t0) * i / nTicks;
    const t = el("text", { x: xs(tv), y: H - B + 12, "text-anchor": "middle" }, chart);
    t.textContent = fmtClock(Math.round(tv));
  }
  el("line", { x1: Lm, y1: ys(P.ref_current_a), x2: W - Rm, y2: ys(P.ref_current_a),
    class: "limit-line" }, chart);
  const rt = el("text", { x: W - Rm, y: ys(P.ref_current_a) - 2, "text-anchor": "end" }, chart);
  rt.textContent = "参考 " + P.ref_current_a + " A";

  // 电流曲线 + 告警点
  el("polyline", { points: samples.map(s => xs(s.t) + "," + ys(s.current)).join(" "),
    class: "field-line" }, chart);
  for (const s of samples) {
    if (s.clip) el("circle", { cx: xs(s.t), cy: ys(s.current), r: 2.6,
      fill: "#d24040" }, chart)
      .appendChild(document.createElementNS(SVGNS, "title"))
      .textContent = "削波 @ " + fmtClock(s.t);
    if (s.overheat) el("circle", { cx: xs(s.t), cy: ys(s.current), r: 2.6,
      fill: "#f0932b" }, chart)
      .appendChild(document.createElementNS(SVGNS, "title"))
      .textContent = "过热 @ " + fmtClock(s.t);
  }
  // 锚点竖线
  effAnchors().forEach((a, i) => {
    el("line", { x1: xs(a.amp_t), y1: T, x2: xs(a.amp_t), y2: H - B,
      stroke: "#4da3ff", "stroke-width": 0.7, "stroke-dasharray": "3 2" }, chart);
    const t = el("text", { x: xs(a.amp_t) + 1.5, y: T + 8 + (i % 3) * 8,
      class: "anchor-tag" }, chart);
    t.textContent = "锚" + (i + 1);
    el("title", {}, t).textContent =
      "锚点: 功放 " + fmtClock(a.amp_t) + " ↔ 场强 " + fmtClock(a.field_t) +
      (a.note ? "\n" + a.note : "");
  });
  // 测点映射位置
  for (const p of points()) {
    if (p.amp_t == null) continue;
    const color = PT_COLOR[p.status] || "#888";
    const cy = p.current_a != null ? ys(p.current_a) : H - B - 3;
    const mk = el("circle", { cx: xs(p.amp_t), cy, r: p.label === selPoint ? 4 : 2.8,
      fill: color, stroke: p.kept.length ? "#ffd75e" : "#0e1116",
      "stroke-width": p.kept.length ? 1.4 : 0.6, cursor: "pointer" }, chart);
    el("title", {}, mk).textContent = p.label + " @ " + fmtClock(p.amp_t) +
      (p.current_a != null ? " " + p.current_a.toFixed(2) + " A" : " (断档)") +
      " 校正 " + fmtDb(p.correction_db) + " dB";
    mk.addEventListener("click", () => selectPoint(p.label));
  }
  const cap = el("text", { x: W - Rm, y: H - 2, "text-anchor": "end" }, chart);
  cap.textContent = "功放时钟 / 电流 A";
}

/* ---------------------------------------------------------------- 侧栏 */

function renderRecord() {
  const info = $("#recInfo");
  if (!D) { info.textContent = ""; return; }
  const rec = effRecord();
  if (!rec) { info.textContent = "尚未导入功放记录"; return; }
  info.innerHTML = "";
  info.appendChild(document.createTextNode(
    rec.label + " · " + rec.n_samples + " 样本 · " + fmtClock(rec.t0) +
    "–" + fmtClock(rec.t1) +
    " · 削波 " + rec.n_clip + " / 过热 " + rec.n_overheat +
    (locked() ? " · 已锁定" : "")));
  if (!locked()) {
    const del = document.createElement("span");
    del.className = "del"; del.textContent = " ✕删除";
    del.style.cursor = "pointer";
    del.onclick = async () => {
      D = await api("/api/drive-surveys/" + D.id + "/record", { method: "DELETE" });
      status("功放记录已删除,已重算"); renderAll();
    };
    info.appendChild(del);
  }
  $("#ptInfo").textContent = D.n_point_rows
    ? "场强记录 " + D.n_point_rows + " 行(重导即整体替换)" + (locked() ? " · 已锁定" : "")
    : "尚未导入场强记录";
}

function renderAnchors() {
  const ul = $("#anchorList");
  ul.innerHTML = "";
  if (!D) return;
  const anchors = effAnchors();
  if (!anchors.length) ul.innerHTML = "<li class='dim'>尚未绑定锚点(至少 2 个)</li>";
  anchors.forEach((a, i) => {
    const li = document.createElement("li");
    li.innerHTML = "<span class='tag'>锚" + (i + 1) + "</span>功放 " + fmtClock(a.amp_t) +
      " ↔ 场强 " + fmtClock(a.field_t) +
      "<span class='dim'> " + esc(a.note || "") + "</span>" +
      (locked() ? "" : "<span class='del' title='删除锚点(换绑,需备注)'>✕</span>");
    li.querySelector(".del")?.addEventListener("click", async () => {
      const note = prompt("删除该锚点属于换绑,请填写理由(将形成新修订):");
      if (!note) { status("已取消:换绑必须备注理由"); return; }
      D = await api("/api/drive-anchors/" + a.id, {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      status("锚点已删除,已切换至修订 " + D.revision); renderAll();
    });
    ul.appendChild(li);
  });
  const mp = effResult()?.mapping;
  if (mp && anchors.length >= 2)
    $("#anchorList").insertAdjacentHTML("beforeend",
      "<li class='" + (mp.ok ? "dim" : "issue") + "'>分段映射 " + mp.segments.length +
      " 段" + (mp.ok ? " · 单调有效" : " · ⚠ 存在映射倒退段") + "</li>");
}

function renderKeeps() {
  const ul = $("#keepList");
  ul.innerHTML = "";
  if (!D) return;
  const keeps = effKeeps();
  if (!keeps.length) ul.innerHTML = "<li class='dim'>暂无保留段</li>";
  for (const k of keeps) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='tag'>" + (KEEP_TEXT[k.kind] || k.kind) + "</span>" +
      fmtClock(k.t0) + "–" + fmtClock(k.t1) +
      "<span class='dim'> " + esc(k.note) + "</span>" +
      (locked() ? "" : "<span class='del' title='撤销保留段(需备注)'>✕</span>");
    li.querySelector(".del")?.addEventListener("click", async () => {
      const note = prompt("撤销该保留段的理由(将形成新修订):");
      if (!note) { status("已取消:撤销保留段必须备注理由"); return; }
      D = await api("/api/drive-keeps/" + k.id, {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      status("保留段已撤销,已切换至修订 " + D.revision); renderAll();
    });
    ul.appendChild(li);
  }
}

function renderParams() {
  if (!D) return;
  document.querySelectorAll("#paramsForm input").forEach(inp => {
    inp.value = D.params[inp.dataset.k];
    inp.disabled = locked();
  });
}

function renderEvents() {
  const ul = $("#eventList");
  ul.innerHTML = "";
  if (!D) return;
  if (!D.events.length) ul.innerHTML = "<li class='dim'>暂无事件</li>";
  for (const e of D.events) {
    const li = document.createElement("li");
    const p = e.payload;
    let txt = EVENT_TEXT[e.kind] || e.kind;
    if (e.kind === "import-record") txt += " " + p.rows + " 样本";
    else if (e.kind === "import-points") txt += " " + p.rows + " 行";
    else if (e.kind === "anchor") txt += " " + fmtClock(p.amp_t) + "↔" + fmtClock(p.field_t);
    else if (e.kind === "keep") txt += " " + (KEEP_TEXT[p.kind] || p.kind);
    li.innerHTML = "<span class='tag'>修订" + e.revision + "</span>" + esc(txt) +
      "<span class='dim' style='margin-left:auto'>" + e.created_at.slice(5, 16) + "</span>";
    li.title = p.note || JSON.stringify(p);
    ul.appendChild(li);
  }
}

function renderStats() {
  const box = $("#stats");
  const r = effResult();
  if (!D || !r) { box.innerHTML = "<span class='dim'>尚未计算</span>"; return; }
  const s = r.stats;
  const chips = [
    ["#1c6dd9", "进入覆盖 " + s.n_ok],
    ["#9b59b6", "排除 " + s.n_excluded],
    ["#ffd75e", "保留段豁免 " + s.n_kept],
    ["#4da3ff", "锚点 " + s.n_anchors],
  ];
  for (const [code, n] of Object.entries(s.reasons))
    chips.push(["#9b59b6", ncReason(code) + " " + n]);
  if (r.samples_summary.gaps.length)
    chips.push(["#d24040", "断档 " + r.samples_summary.gaps.length + " 处"]);
  box.innerHTML = "<div class='chips'>" + chips.map(c =>
    "<span class='chip' style='background:" + c[0] + "'>" + c[1] + "</span>").join("") +
    "</div>" + (s.map_ok ? "" : "<p class='issue'>时码映射无效或存在倒退段</p>");
}

function renderSnapList() {
  const ul = $("#snapList");
  ul.innerHTML = "";
  if (!D) return;
  const snaps = D.snapshots || [];
  if (!snaps.length) { ul.innerHTML = "<li class='dim'>暂无快照</li>"; return; }
  for (const s of snaps) {
    const li = document.createElement("li");
    const cur = s.revision === D.revision;
    if (snapView && snapView.revision === s.revision) li.classList.add("sel");
    li.innerHTML = "<span class='tag'>修订" + s.revision + "</span>" +
      "合格 " + s.n_ok + " / 排除 " + s.n_excluded +
      (cur ? "<span class='tag'>当前</span>" : "") +
      "<span class='dim' style='margin-left:auto'>" + s.created_at.slice(5, 16) + "</span>";
    li.title = cur ? "当前修订" : "点击查看该修订的只读快照";
    li.onclick = () => { if (!cur) viewSnapshot(s.revision); else backToCurrent(); };
    ul.appendChild(li);
  }
}

async function viewSnapshot(rev) {
  snapView = await api("/api/drive-surveys/" + D.id + "/revisions/" + rev);
  selPoint = null;
  renderAll();
  status("正在查看历史修订 " + rev + " 的只读快照(锚点/保留段/测点状态均为当时值)");
}

function backToCurrent() {
  if (!snapView) return;
  snapView = null;
  selPoint = null;
  renderAll();
  status("已返回当前修订 " + D.revision);
}

function renderPointList() {
  const ul = $("#pointList");
  ul.innerHTML = "";
  if (!D || !effResult()) return;
  const ps = points();
  $("#ptCount").textContent = "(" + ps.length + ")";
  for (const p of ps) {
    const li = document.createElement("li");
    if (p.label === selPoint) li.classList.add("sel");
    li.innerHTML = "<span class='dot' style='background:" + PT_COLOR[p.status] + "'></span>" +
      esc(p.label) +
      (p.kept.length ? "<span class='tag'>保留</span>" : "") +
      "<span class='dim' style='margin-left:auto'>" + fmtDb(p.raw_ref_db) + " → " +
      fmtDb(p.norm_ref_db) + " dB</span>";
    li.title = p.reasons.length ? p.reasons.map(ncReason).join(";")
      : "校正 " + fmtDb(p.correction_db) + " dB · 电流 " +
        (p.current_a != null ? p.current_a.toFixed(2) + " A" : "—");
    li.onclick = () => selectPoint(p.label);
    ul.appendChild(li);
  }
}

function renderDetail() {
  const box = $("#detail");
  if (!D || !effResult()) { box.innerHTML = "<p class='dim'>在座位图或时间曲线上点击测点</p>"; return; }
  const p = points().find(q => q.label === selPoint);
  if (!p) { box.innerHTML = "<p class='dim'>在座位图或时间曲线上点击测点</p>"; return; }
  let html = "<table>" +
    "<tr><td>测点</td><td>" + esc(p.label) + " @ (" + p.x + ", " + p.y + ")</td></tr>" +
    "<tr><td>场强时刻</td><td>" + esc(p.t_text) + " (" + fmtClock(p.field_t) + ")</td></tr>" +
    "<tr><td>映射功放时刻</td><td>" + fmtClock(p.amp_t) + "</td></tr>" +
    "<tr><td>环路电流</td><td>" + (p.current_a != null ? p.current_a.toFixed(3) + " A" : "—") +
    " (参考 " + p.ref_current_a + " A)</td></tr>" +
    "<tr><td>校正值</td><td>" + fmtDb(p.correction_db) + " dB</td></tr>" +
    "<tr><td>参考频点 " + p.ref_freq + " Hz</td><td>原始 " + fmtDb(p.raw_ref_db) +
    " → 归一 " + fmtDb(p.norm_ref_db) + " dB</td></tr>" +
    "<tr><td>状态</td><td>" + (p.status === "ok" ? "进入覆盖计算" :
      "<span class='issue'>排除:" + p.reasons.map(ncReason).join(";") + "</span>") +
    (p.kept.length ? "<br>保留段豁免:" + p.kept.map(ncReason).join(";") : "") + "</td></tr>";
  if (p.sample_lo) html += "<tr><td>电流样本</td><td>" + fmtClock(p.sample_lo.t) + " " +
    p.sample_lo.current.toFixed(2) + "A ~ " + fmtClock(p.sample_hi.t) + " " +
    p.sample_hi.current.toFixed(2) + "A</td></tr>";
  html += "</table><table><tr><td>频点 Hz</td><td>原始 dB</td><td>校正 dB</td><td>归一 dB</td></tr>";
  for (const [f, v] of Object.entries(p.freqs).sort((a, b) => a[0] - b[0]))
    html += "<tr><td>" + f + "</td><td>" + fmtDb(v.raw) + "</td><td>" + fmtDb(v.correction) +
      "</td><td>" + fmtDb(v.norm) + "</td></tr>";
  html += "</table>";
  box.innerHTML = html;
}

function selectPoint(label) {
  selPoint = label;
  renderViewport(); drawChart(); renderPointList(); renderDetail();
  const p = points().find(q => q.label === label);
  if (p) status(label + ": 原始 " + fmtDb(p.raw_ref_db) + " → 归一 " + fmtDb(p.norm_ref_db) +
    " dB · 电流 " + (p.current_a != null ? p.current_a.toFixed(2) + " A" : "—") +
    (p.reasons.length ? " · 排除:" + p.reasons.map(ncReason).join(";") : " · 进入覆盖计算"));
}

/* ---------------------------------------------------------------- 复测对照 */

function renderComparePanel() {
  const confirmed = surveys.filter(s => s.status === "confirmed");
  for (const id of ["#cmpBase", "#cmpRetest"]) {
    const sel = $(id);
    const cur = sel.value;
    sel.innerHTML = "";
    if (!confirmed.length) {
      const o = document.createElement("option");
      o.textContent = "(无已确认版本)";
      sel.appendChild(o);
    }
    for (const s of confirmed) {
      const o = document.createElement("option");
      o.value = s.id;
      o.textContent = "#" + s.id + " " + s.label + " 修订" + s.revision;
      sel.appendChild(o);
    }
    if (cur) sel.value = cur;
  }
  $("#cmpRetest").value = (confirmed[1] || confirmed[0] || {}).id || "";
  const ul = $("#cmpList");
  ul.innerHTML = "";
  if (!compares.length) ul.innerHTML = "<li class='dim'>暂无对照</li>";
  for (const c of compares) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='tag'>对照</span>" + esc(c.label) +
      "<span class='dim' style='margin-left:auto'>" +
      (c.stats ? "配对 " + c.stats.n_pairs + " · 最大差 " +
        (c.stats.max_abs_delta_db == null ? "—" : c.stats.max_abs_delta_db + " dB") : "") +
      "</span>";
    li.onclick = () => loadCompare(c.id);
    ul.appendChild(li);
  }
}

async function loadCompare(cid) {
  const c = await api("/api/drive-compares/" + cid);
  const ul = $("#cmpDetail");
  ul.innerHTML = "";
  $("#cmpTitle").textContent = "#" + c.base_survey_id + " → #" + c.retest_survey_id +
    "(归一化参考频点场强差)";
  const r = c.result;
  if (!r) return;
  for (const p of r.pairs) {
    const li = document.createElement("li");
    const cls = Math.abs(p.delta_db) > 3 ? "issue" : "dim";
    li.innerHTML = esc(p.label) + "<span class='" + cls + "' style='margin-left:auto'>" +
      fmtDb(p.base_norm_db) + " → " + fmtDb(p.retest_norm_db) + " dB(Δ " +
      (p.delta_db > 0 ? "+" : "") + p.delta_db + ")</span>";
    li.title = "原始 " + fmtDb(p.base_raw_db) + " → " + fmtDb(p.retest_raw_db) +
      " dB · 电流 " + fmtDb(p.base_current_a) + " → " + fmtDb(p.retest_current_a) + " A";
    ul.appendChild(li);
  }
  for (const s of r.skipped) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='dim'>" + esc(s.label) + "(跳过:" +
      (s.base_status !== "ok" ? "基准被排除" : "") +
      (s.retest_status !== "ok" ? "复测被排除" : "") + ")</span>";
    ul.appendChild(li);
  }
  if (!ul.children.length) ul.innerHTML = "<li class='dim'>无共同测点</li>";
  status("对照 " + c.label + ": 配对 " + r.stats.n_pairs + " · 最大差 " +
    (r.stats.max_abs_delta_db == null ? "—" : r.stats.max_abs_delta_db + " dB"));
}

/* ---------------------------------------------------------------- 交互 */

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

$("#btnCreate").onclick = async () => {
  const st = await api("/api/projects/" + projectId + "/drive-surveys", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label: $("#drvLabel").value }),
  });
  status("已新建驱动基准校审 #" + st.id);
  $("#drvLabel").value = "";
  await refreshSurveyList(st.id);
};

$("#drvSelect").onchange = (ev) => loadSurvey(+ev.target.value);

async function importCsv(fileInput, url, labelInput) {
  const f = $(fileInput).files[0];
  if (!f) { status("请先选择 CSV 文件"); return; }
  const fd = new FormData();
  fd.append("csv", f);
  if (labelInput && $(labelInput).value) fd.append("label", $(labelInput).value);
  D = await api(url, { method: "POST", body: fd });
  snapView = null;
  const r = D.import_result;
  status("已导入 " + r.rows + " 行,已重算" +
    (r.csv_errors.length ? ";CSV 警告 " + r.csv_errors.length + " 条" : ""));
  $(fileInput).value = "";
  if (labelInput) $(labelInput).value = "";
  renderAll();
}

$("#btnImportRec").onclick = () =>
  importCsv("#recFile", "/api/drive-surveys/" + D.id + "/record", "#recLabel");
$("#btnImportPts").onclick = () =>
  importCsv("#ptFile", "/api/drive-surveys/" + D.id + "/points", null);

$("#btnAnchor").onclick = async () => {
  const note = $("#ancNote").value.trim();
  if (!note) { status("绑定/换绑锚点必须填写理由"); return; }
  D = await api("/api/drive-surveys/" + D.id + "/anchors", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amp_t: $("#ancAmp").value.trim(),
      field_t: $("#ancField").value.trim(), note }),
  });
  snapView = null;
  $("#ancAmp").value = $("#ancField").value = $("#ancNote").value = "";
  status("锚点已绑定,已切换至修订 " + D.revision);
  renderAll();
};

$("#btnKeep").onclick = async () => {
  const note = $("#keepNote").value.trim();
  if (!note) { status("保留异常段必须填写理由"); return; }
  D = await api("/api/drive-surveys/" + D.id + "/keeps", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: $("#keepKind").value,
      t0: $("#keepT0").value.trim(), t1: $("#keepT1").value.trim(), note }),
  });
  snapView = null;
  $("#keepT0").value = $("#keepT1").value = $("#keepNote").value = "";
  status("保留段已记录,已切换至修订 " + D.revision);
  renderAll();
};

$("#btnSaveParams").onclick = async () => {
  const data = {};
  document.querySelectorAll("#paramsForm input").forEach(inp => data[inp.dataset.k] = inp.value);
  D = await api("/api/drive-surveys/" + D.id + "/params", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  snapView = null;
  status("判定参数已保存,已重算");
  renderAll();
};

$("#btnConfirm").onclick = async () => {
  try {
    D = await api("/api/drive-surveys/" + D.id + "/confirm", { method: "POST" });
    snapView = null;
    status("校审已确认:记录、锚点、保留段与参数已锁定,可导出三份材料");
    renderAll(); refreshSurveyList(D.id);
  } catch (e) { /* 已提示 */ }
};

$("#btnReopen").onclick = async () => {
  const note = prompt("重审原因(修订号 +1):", "驱动条件复核后重审");
  if (note == null) return;
  D = await api("/api/drive-surveys/" + D.id + "/reopen", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
  snapView = null;
  status("已解除锁定,修订号 +1;调整后请重新确认");
  renderAll(); refreshSurveyList(D.id);
};

$("#btnCmpCreate").onclick = async () => {
  try {
    await api("/api/projects/" + projectId + "/drive-compares", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_survey_id: +$("#cmpBase").value,
        retest_survey_id: +$("#cmpRetest").value }),
    });
    status("驱动对照已生成(引用已确认版本的冻结结果)");
    await refreshCompares();
  } catch (e) { /* 已提示 */ }
};

function exportUrl(kind) {
  if (snapView)
    return "/api/drive-surveys/" + D.id + "/revisions/" + snapView.revision + "/export/" + kind;
  return "/api/drive-surveys/" + D.id + "/export/" + kind;
}

$("#btnExpSvg").onclick = () => D && (location = exportUrl("drive.svg"));
$("#btnExpCsv").onclick = () => D && (location = exportUrl("points.csv"));
$("#btnExpJson").onclick = () => D && (location = exportUrl("recalc.json"));
$("#btnSnapExportSvg").onclick = () => snapView && (location = exportUrl("drive.svg"));
$("#btnSnapExportCsv").onclick = () => snapView && (location = exportUrl("points.csv"));
$("#btnSnapExportJson").onclick = () => snapView && (location = exportUrl("recalc.json"));
$("#btnSnapBack").onclick = backToCurrent;

boot();
