/* 助听感应环巡测校审 — 前端(原生 JS + SVG)
 * 座位图 / 网格覆盖 / 分区绘制 / 测点拖动 / 频谱联动 / 补测路径
 */
"use strict";

const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

const STATUS_COLOR = { ok: "#2e9e5b", warn: "#d8a012", fail: "#d24040",
                       nodata: "#8a8f98", noconclusion: "#9b59b6", notest: "#3a3f47" };
const STATUS_TEXT = { ok: "合格", warn: "临近干扰", fail: "不合格",
                      nodata: "证据不足", noconclusion: "不作结论", notest: "禁测区" };
const ZONE_TEXT = { audience: "观众区", notest: "禁测区", interference: "干扰设备" };

let S = null;            // 服务端状态
let projectId = null;
let tool = "select";     // select | pan | audience | notest | interference
let selPoint = null;     // 选中测点 label
let selCell = null;      // 选中网格 {cx,cy}
let polyDraft = [];      // 正在绘制的多边形顶点
let pathOverlay = null;  // 补测路径
let drag = null;         // 拖动状态

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

function svgPoint(clientX, clientY) {
  const pt = viewport.createSVGPoint();
  pt.x = clientX; pt.y = clientY;
  return pt.matrixTransform(viewport.getScreenCTM().inverse());
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { status("错误:" + (data.error || r.status)); throw new Error(data.error || r.status); }
  return data;
}

function issueText(code) {
  const [k, arg] = code.split(":");
  const t = (S.issue_text && S.issue_text[k]) || k;
  return arg ? t + "(" + arg + ")" : t;
}

/* ---------------------------------------------------------------- 初始化 */

async function boot() {
  const projects = await api("/api/projects");
  if (projects.length) {
    projectId = projects[0].id;
    await loadState();
    $("#main").classList.remove("hidden");
  } else {
    $("#setup").classList.remove("hidden");
  }
}

async function loadState(versionId) {
  const q = versionId ? "?version_id=" + versionId : "";
  S = await api("/api/projects/" + projectId + "/state" + q);
  if (S.import_result) showImportResult(S.import_result);
  renderAll();
}

function showImportResult(r) {
  const parts = [];
  if (r.added.length) parts.push("新增测点 " + r.added.length + " 个");
  if (r.replaced.length) parts.push("复测替换 " + r.replaced.length + " 个:" + r.replaced.join(","));
  if (r.skipped_locked.length) parts.push("已锁定跳过 " + r.skipped_locked.length + " 个:" + r.skipped_locked.join(","));
  if (r.csv_errors.length) parts.push("CSV 警告:\n" + r.csv_errors.slice(0, 8).join("\n"));
  $("#importMsg").textContent = parts.join("\n") || "导入完成";
}

/* ---------------------------------------------------------------- 总渲染 */

function renderAll() {
  $("#projName").textContent = S.project.name;
  renderVersionBar();
  renderViewport();
  renderLimitsForm();
  renderZoneList();
  renderPointList();
  renderDecisionList();
  renderStats();
  renderDetail();
  drawSpectrum();
  renderLegend();
}

function renderVersionBar() {
  const sel = $("#versionSelect");
  sel.innerHTML = "";
  for (const v of S.versions) {
    const o = document.createElement("option");
    o.value = v.id;
    o.textContent = "#" + v.id + " " + v.label + " (" + v.created_at + ")";
    if (S.version && v.id === S.version.id) o.selected = true;
    sel.appendChild(o);
  }
  const b = $("#calibBadge");
  const cs = S.version && S.version.calibration_summary;
  if (cs) {
    b.textContent = "校准 " + cs.devices.map(d => d.device_id + "/" + d.calib_version).join(" · ")
      + (cs.mixed_calib ? " ⚠混用" : "");
    b.classList.toggle("mixed", !!cs.mixed_calib);
    b.title = "测点 " + cs.n_points + " / 有效 " + cs.n_valid +
      " / 锁定 " + cs.n_locked + " / 排除 " + cs.n_excluded;
  } else b.textContent = "";
}

/* ---------------------------------------------------------------- 主视图 */

function renderViewport() {
  viewport.innerHTML = "";
  const b = S.project.bounds;
  const mx = b.width * 0.06, my = b.height * 0.06;
  viewport.setAttribute("viewBox", [b.min_x - mx, b.min_y - my,
    b.width + 2 * mx, b.height + 2 * my].join(" "));

  // 场地 SVG 底图
  const gVenue = el("g", { id: "venueLayer", opacity: 0.5 }, viewport);
  try {
    const doc = new DOMParser().parseFromString(S.project.venue_svg, "image/svg+xml");
    const root = doc.querySelector("svg");
    if (root) for (const n of [...root.childNodes]) gVenue.appendChild(document.importNode(n, true));
  } catch (e) { /* 底图解析失败不阻断 */ }

  el("g", { id: "gridLayer" }, viewport);
  el("g", { id: "zoneLayer" }, viewport);
  el("g", { id: "pathLayer" }, viewport);
  el("g", { id: "pointLayer" }, viewport);
  el("g", { id: "draftLayer" }, viewport);

  renderGrid();
  renderZones();
  renderPoints();
  if (pathOverlay) drawPath(pathOverlay);
}

function renderGrid() {
  const g = $("#gridLayer");
  if (!g || !S.grid) return;
  const cs = S.grid.cs;
  for (const c of S.cells) {
    const r = el("rect", {
      x: c.x - cs / 2, y: c.y - cs / 2, width: cs, height: cs,
      fill: STATUS_COLOR[c.status] || "#888",
      "fill-opacity": c.status === "notest" ? 0.35 : 0.5,
      stroke: "none", "data-cx": c.cx, "data-cy": c.cy,
    }, g);
    if (c.status === "nodata") {  // 证据不足:虚线框标出
      el("rect", { x: c.x - cs / 2, y: c.y - cs / 2, width: cs, height: cs,
        fill: "none", stroke: "#c8ccd4", "stroke-width": 0.12,
        "stroke-dasharray": "0.5 0.4" }, g);
    }
    if (selCell && selCell.cx === c.cx && selCell.cy === c.cy)
      r.setAttribute("stroke", "#fff"), r.setAttribute("stroke-width", 0.3);
    r.addEventListener("mousemove", (ev) => showCellTip(ev, c));
    r.addEventListener("mouseleave", hideTip);
    r.addEventListener("click", (ev) => {
      if (tool !== "select") return;
      ev.stopPropagation();
      selCell = { cx: c.cx, cy: c.cy }; selPoint = null;
      renderDetail(); drawSpectrum(); renderViewport();
    });
  }
}

function showCellTip(ev, c) {
  const t = $("#tooltip");
  t.innerHTML = "";
  const rows = [["状态", STATUS_TEXT[c.status] || c.status]];
  if (c.field != null) rows.push(["场强", c.field + " dB"]);
  if (c.snr != null) rows.push(["信噪比", c.snr + " dB"]);
  if (c.uniformity != null) rows.push(["均匀度", c.uniformity + " dB"]);
  if (c.freq_dev != null) rows.push(["频响偏差", c.freq_dev + " dB"]);
  rows.push(["测点数", c.n]);
  if (c.reason) rows.push(["说明", c.reason]);
  t.innerHTML = rows.map(r => "<div><b>" + r[0] + "</b> " + r[1] + "</div>").join("");
  const wrap = $("#viewportWrap").getBoundingClientRect();
  t.style.left = (ev.clientX - wrap.left + 14) + "px";
  t.style.top = (ev.clientY - wrap.top + 10) + "px";
  t.classList.remove("hidden");
}
function hideTip() { $("#tooltip").classList.add("hidden"); }

function renderZones() {
  const g = $("#zoneLayer");
  if (!g) return;
  const style = { audience: "#2e9e5b", notest: "#8a8f98", interference: "#d8a012" };
  for (const z of S.zones) {
    const pts = z.polygon.map(p => p[0] + "," + p[1]).join(" ");
    const poly = el("polygon", {
      points: pts, fill: style[z.kind], "fill-opacity": z.kind === "audience" ? 0.04 : 0.12,
      stroke: style[z.kind], "stroke-width": 0.3, "stroke-dasharray": "1.4 0.9",
    }, g);
    const t = el("title", {}, poly); t.textContent = ZONE_TEXT[z.kind] + " " + (z.name || "");
    const c = centroid(z.polygon);
    const label = el("text", { x: c[0], y: c[1], "font-size": 2.2, fill: style[z.kind],
      "text-anchor": "middle", "pointer-events": "none" }, g);
    label.textContent = (ZONE_TEXT[z.kind] || "") + (z.name ? "·" + z.name : "");
  }
}

function centroid(poly) {
  let x = 0, y = 0;
  for (const p of poly) { x += p[0]; y += p[1]; }
  return [x / poly.length, y / poly.length];
}

function renderPoints() {
  const g = $("#pointLayer");
  if (!g) return;
  for (const p of S.points) {
    const grp = el("g", { "data-label": p.label, cursor: tool === "select" ? "move" : "default" }, g);
    let fill = "#1c6dd9";
    if (!p.valid) fill = "#d24040";
    if (p.excluded) fill = "#5a6068";
    const c = el("circle", {
      cx: p.x, cy: p.y, r: selPoint === p.label ? 0.95 : 0.62,
      fill, stroke: selPoint === p.label ? "#fff" : "rgba(255,255,255,.75)",
      "stroke-width": selPoint === p.label ? 0.3 : 0.15,
    }, grp);
    if (p.locked) el("circle", { cx: p.x, cy: p.y, r: 1.05, fill: "none",
      stroke: "#ffd75e", "stroke-width": 0.28 }, grp);
    if (p.excluded) {
      el("line", { x1: p.x - 0.7, y1: p.y - 0.7, x2: p.x + 0.7, y2: p.y + 0.7,
        stroke: "#c8ccd4", "stroke-width": 0.25 }, grp);
      el("line", { x1: p.x - 0.7, y1: p.y + 0.7, x2: p.x + 0.7, y2: p.y - 0.7,
        stroke: "#c8ccd4", "stroke-width": 0.25 }, grp);
    }
    const txt = el("text", { x: p.x + 0.9, y: p.y - 0.7, "font-size": 1.6,
      fill: "#aeb8c6", "pointer-events": "none" }, grp);
    txt.textContent = p.label;
    const title = el("title", {}, grp);
    title.textContent = p.label + (p.valid ? "" : " ⚠" + p.issues.map(issueText).join(";"))
      + (p.excluded ? " [已排除] " + p.exclude_reason : "") + (p.locked ? " [已锁定]" : "");
    grp.addEventListener("mousedown", (ev) => onPointDown(ev, p, grp));
    grp.addEventListener("click", (ev) => {
      if (tool !== "select") return;
      ev.stopPropagation();
      selPoint = p.label; selCell = null;
      renderDetail(); drawSpectrum(); renderPointList(); renderViewport();
    });
  }
}

/* ---------------------------------------------------------------- 交互:拖动 / 平移 / 缩放 / 绘制 */

function onPointDown(ev, p, grp) {
  if (tool !== "select") return;
  if (p.locked) { status("测点 " + p.label + " 已锁定(已复核),不可移动"); return; }
  ev.preventDefault();
  drag = { kind: "point", label: p.label, grp, moved: false };
}

viewport.addEventListener("mousedown", (ev) => {
  if (tool === "pan") {
    drag = { kind: "pan", sx: ev.clientX, sy: ev.clientY,
             vb: { ...viewport.viewBox.baseVal } };
    viewport.classList.add("panning");
  }
});

viewport.addEventListener("click", (ev) => {
  if (tool === "audience" || tool === "notest") {
    const p = svgPoint(ev.clientX, ev.clientY);
    polyDraft.push([+p.x.toFixed(2), +p.y.toFixed(2)]);
    drawDraft();
  } else if (tool === "interference") {
    const p = svgPoint(ev.clientX, ev.clientY);
    const name = $("#zoneName").value || "调光设备";
    const poly = [];
    for (let i = 0; i < 12; i++) {
      const a = i / 12 * 2 * Math.PI;
      poly.push([+(p.x + 2.5 * Math.cos(a)).toFixed(2), +(p.y + 2.5 * Math.sin(a)).toFixed(2)]);
    }
    addZone("interference", name, poly);
  } else if (tool === "select" && ev.target === viewport) {
    selPoint = null; selCell = null;
    renderDetail(); drawSpectrum(); renderPointList(); renderViewport();
  }
});

viewport.addEventListener("dblclick", (ev) => {
  if ((tool === "audience" || tool === "notest") && polyDraft.length >= 3) finishPoly();
});

window.addEventListener("mousemove", (ev) => {
  if (!drag) return;
  if (drag.kind === "pan") {
    const vb = viewport.viewBox.baseVal;
    const k = vb.width / viewport.clientWidth;
    vb.x = drag.vb.x - (ev.clientX - drag.sx) * k;
    vb.y = drag.vb.y - (ev.clientY - drag.sy) * k;
  } else if (drag.kind === "point") {
    const p = svgPoint(ev.clientX, ev.clientY);
    drag.moved = true;
    drag.x = +p.x.toFixed(2); drag.y = +p.y.toFixed(2);
    const c = drag.grp.querySelector("circle");
    c.setAttribute("cx", drag.x); c.setAttribute("cy", drag.y);
  }
});

window.addEventListener("mouseup", async () => {
  if (!drag) return;
  const d = drag; drag = null;
  viewport.classList.remove("panning");
  if (d.kind === "point" && d.moved && S.version) {
    try {
      S = await api("/api/versions/" + S.version.id + "/move", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ point_label: d.label, x: d.x, y: d.y }),
      });
      status("测点 " + d.label + " 已移动到 (" + d.x + ", " + d.y + "),相关网格已重算");
      renderAll();
    } catch (e) { renderAll(); }
  }
});

viewport.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const vb = viewport.viewBox.baseVal;
  const p = svgPoint(ev.clientX, ev.clientY);
  const k = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
  vb.x = p.x - (p.x - vb.x) * k;
  vb.y = p.y - (p.y - vb.y) * k;
  vb.width *= k; vb.height *= k;
}, { passive: false });

function drawDraft() {
  const g = $("#draftLayer");
  g.innerHTML = "";
  if (!polyDraft.length) return;
  el("polyline", { points: polyDraft.map(p => p.join(",")).join(" "),
    fill: "none", stroke: "#4da3ff", "stroke-width": 0.3, "stroke-dasharray": "1 0.6" }, g);
  for (const p of polyDraft)
    el("circle", { cx: p[0], cy: p[1], r: 0.4, fill: "#4da3ff" }, g);
}

async function finishPoly() {
  if (polyDraft.length < 3) return;
  const name = $("#zoneName").value || (tool === "audience" ? "观众席" : "禁测区");
  await addZone(tool, name, polyDraft);
  polyDraft = [];
  $("#polyBar").classList.add("hidden");
}

async function addZone(kind, name, polygon) {
  S = await api("/api/projects/" + projectId + "/zones", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, name, polygon }),
  });
  status("已添加" + ZONE_TEXT[kind] + "「" + name + "」,网格已全量重算");
  renderAll();
}

/* ---------------------------------------------------------------- 频谱 */

function drawSpectrum() {
  spectrum.innerHTML = "";
  const W = 340, H = 230, L = 38, R = 8, T = 14, B = 26;
  const p = S && S.points.find(q => q.label === selPoint);
  $("#spectrumTitle").textContent = p ? "测点 " + p.label : "";
  const lim = S ? S.limits : null;
  const freqs = lim ? lim.expected_freqs : [100, 500, 1000, 2000, 4000, 5000];
  const fmin = Math.min(...freqs) / 1.4, fmax = Math.max(...freqs) * 1.4;
  const xs = (f) => L + (Math.log10(f) - Math.log10(fmin)) / (Math.log10(fmax) - Math.log10(fmin)) * (W - L - R);

  let lo = -45, hi = 5;
  if (p) {
    const vals = Object.values(p.freqs).flatMap(v => [v.field, v.noise]);
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
  cap.textContent = "Hz / dB  绿:场强 灰:背景噪声 红:限值";

  if (lim) {  // 场强限值带
    el("rect", { x: L, y: ys(lim.field_max), width: W - L - R,
      height: ys(lim.field_min) - ys(lim.field_max), class: "band" }, spectrum);
    for (const v of [lim.field_min, lim.field_max])
      el("line", { x1: L, y1: ys(v), x2: W - R, y2: ys(v), class: "limit-line" }, spectrum);
  }
  if (!p) {
    const t = el("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, spectrum);
    t.textContent = "在座位图上点击测点查看频响";
    return;
  }
  const fkeys = Object.keys(p.freqs).map(Number).sort((a, b) => a - b);
  const line = (key, cls) => {
    const pts = fkeys.map(f => xs(f) + "," + ys(p.freqs[f][key])).join(" ");
    el("polyline", { points: pts, class: cls }, spectrum);
  };
  line("noise", "noise-line");
  line("field", "field-line");
  const refF = lim ? lim.ref_freq : 1000;
  const refKey = fkeys.reduce((a, b) => Math.abs(b - refF) < Math.abs(a - refF) ? b : a, fkeys[0]);
  const refLvl = p.freqs[refKey].field;
  if (lim) {  // 频响偏差带(相对参考频点)
    el("rect", { x: L, y: ys(refLvl + lim.freq_dev_db), width: W - L - R,
      height: ys(refLvl - lim.freq_dev_db) - ys(refLvl + lim.freq_dev_db),
      fill: "rgba(77,163,255,.10)" }, spectrum);
  }
  for (const f of fkeys) {
    const c1 = el("circle", { cx: xs(f), cy: ys(p.freqs[f].field), r: 3.4 }, spectrum);
    el("title", {}, c1).textContent = f + " Hz 场强 " + p.freqs[f].field + " dB";
    const c2 = el("circle", { cx: xs(f), cy: ys(p.freqs[f].noise), r: 3, class: "noise" }, spectrum);
    el("title", {}, c2).textContent = f + " Hz 噪声 " + p.freqs[f].noise + " dB";
  }
  const rl = el("text", { x: L + 4, y: ys(refLvl) - 4 }, spectrum);
  rl.textContent = "参考 " + refKey + "Hz: " + refLvl + " dB";
}

/* ---------------------------------------------------------------- 侧栏 */

function renderLimitsForm() {
  const lim = S.limits;
  document.querySelectorAll("#limitsForm input").forEach(inp => {
    const k = inp.dataset.k;
    inp.value = k === "expected_freqs" ? lim.expected_freqs.join(",") : lim[k];
  });
}

function renderZoneList() {
  const ul = $("#zoneList");
  ul.innerHTML = "";
  if (!S.zones.length) ul.innerHTML = "<li class='dim'>尚未圈定分区</li>";
  for (const z of S.zones) {
    const li = document.createElement("li");
    li.innerHTML = "<span class='dot' style='background:" +
      { audience: "#2e9e5b", notest: "#8a8f98", interference: "#d8a012" }[z.kind] +
      "'></span>" + ZONE_TEXT[z.kind] + " " + (z.name || "") +
      "<span class='del' title='删除'>✕</span>";
    li.querySelector(".del").onclick = async () => {
      S = await api("/api/zones/" + z.id, { method: "DELETE" });
      status("分区已删除,网格已全量重算"); renderAll();
    };
    ul.appendChild(li);
  }
}

function renderPointList() {
  const ul = $("#pointList");
  ul.innerHTML = "";
  $("#pointCount").textContent = S.points.length ? "(" + S.points.length + ")" : "";
  for (const p of S.points) {
    const li = document.createElement("li");
    if (p.label === selPoint) li.classList.add("sel");
    const color = p.excluded ? "#5a6068" : (p.valid ? "#1c6dd9" : "#d24040");
    let tags = "";
    if (p.locked) tags += "<span class='tag'>锁</span>";
    if (p.excluded) tags += "<span class='tag'>排除</span>";
    if (!p.valid) tags += "<span class='tag' style='color:#ff8a8a'>无效</span>";
    if (p.moved) tags += "<span class='tag'>已移</span>";
    li.innerHTML = "<span class='dot' style='background:" + color + "'></span>" +
      p.label + tags + "<span class='dim' style='margin-left:auto'>" +
      p.x.toFixed(1) + "," + p.y.toFixed(1) + "</span>";
    li.onclick = () => { selPoint = p.label; selCell = null; renderDetail(); drawSpectrum(); renderPointList(); renderViewport(); };
    ul.appendChild(li);
  }
}

function renderDecisionList() {
  const ul = $("#decisionList");
  ul.innerHTML = "";
  if (!S.decisions.length) ul.innerHTML = "<li class='dim'>暂无人工决定</li>";
  for (const d of S.decisions) {
    const li = document.createElement("li");
    const p = d.payload;
    let txt = d.kind;
    if (d.kind === "move") txt = "移动 " + p.point_label + " → (" + p.to + ")";
    else if (d.kind === "lock") txt = "锁定 " + p.point_label;
    else if (d.kind === "unlock") txt = "解锁 " + p.point_label;
    else if (d.kind === "exclude") txt = "排除 " + p.point_label + ":" + p.reason;
    else if (d.kind === "include") txt = "恢复 " + p.point_label;
    else if (d.kind === "import") txt = "导入「" + p.label + "」 " + p.rows + " 行";
    li.innerHTML = "<span class='tag'>" + (d.version_label || "") + "</span>" + txt;
    li.title = d.created_at;
    ul.appendChild(li);
  }
}

function renderStats() {
  const box = $("#stats");
  const order = ["ok", "warn", "fail", "nodata", "noconclusion", "notest"];
  box.innerHTML = "<div class='chips'>" + order.filter(k => S.stats[k]).map(k =>
    "<span class='chip' style='background:" + STATUS_COLOR[k] + "'>" +
    STATUS_TEXT[k] + " " + S.stats[k] + "</span>").join("") + "</div>"
    || "<span class='dim'>暂无网格数据</span>";
}

function renderLegend() {
  $("#legend").innerHTML = Object.keys(STATUS_TEXT).map(k =>
    "<div class='item'><span class='sw' style='background:" + STATUS_COLOR[k] +
    "'></span>" + STATUS_TEXT[k] + "</div>").join("");
}

function renderDetail() {
  const box = $("#detail");
  if (selPoint) {
    const p = S.points.find(q => q.label === selPoint);
    if (!p) { box.innerHTML = "<p class='dim'>测点不存在</p>"; return; }
    const m = p.metrics || {};
    const rows = [
      ["测点", p.label], ["坐标", p.x.toFixed(2) + ", " + p.y.toFixed(2)],
      ["设备", (p.device_id || "—") + " / " + (p.calib_version || "—")],
      ["场强", m.field != null ? m.field + " dB" : "—"],
      ["信噪比", m.snr != null ? m.snr + " dB" : "—"],
      ["频响偏差", m.freq_dev != null ? m.freq_dev + " dB" : "—"],
      ["频点数", Object.keys(p.freqs).length],
    ];
    let html = "<table>" + rows.map(r => "<tr><td>" + r[0] + "</td><td>" + r[1] + "</td></tr>").join("") + "</table>";
    if (p.issues.length)
      html += "<p class='issue'>⚠ " + p.issues.map(issueText).join(";") + " — 该点不参与任何区域结论</p>";
    if (p.excluded) html += "<p class='issue'>已排除:" + p.exclude_reason + "</p>";
    html += "<div class='actions'>";
    html += p.locked
      ? "<button data-act='unlock'>解除锁定</button>"
      : "<button data-act='lock'>锁定(复核完成)</button>";
    html += p.excluded
      ? "<button data-act='include'>恢复计入</button>"
      : "<button data-act='exclude'>排除(临时异常)</button>";
    html += "</div><input id='exReason' placeholder='排除理由,如:调光设备满载测试干扰'>";
    box.innerHTML = html;
    box.querySelectorAll("button[data-act]").forEach(b =>
      b.onclick = () => decide(p.label, b.dataset.act));
  } else if (selCell) {
    const c = S.cells.find(q => q.cx === selCell.cx && q.cy === selCell.cy);
    if (!c) { box.innerHTML = "<p class='dim'>该格无数据</p>"; return; }
    const rows = [
      ["网格", "(" + c.cx + ", " + c.cy + ") 中心 " + c.x + ", " + c.y],
      ["状态", STATUS_TEXT[c.status] || c.status],
      ["场强", c.field != null ? c.field + " dB" : "—"],
      ["信噪比", c.snr != null ? c.snr + " dB" : "—"],
      ["均匀度", c.uniformity != null ? c.uniformity + " dB" : "—"],
      ["频响偏差", c.freq_dev != null ? c.freq_dev + " dB" : "—"],
      ["贡献测点", c.n],
      ["说明", c.reason || "—"],
    ];
    box.innerHTML = "<table>" + rows.map(r => "<tr><td>" + r[0] + "</td><td>" + r[1] + "</td></tr>").join("") + "</table>";
  } else {
    box.innerHTML = "<p class='dim'>点击测点或网格查看详情</p>";
  }
}

async function decide(label, action) {
  const reason = ($("#exReason") && $("#exReason").value || "").trim();
  try {
    S = await api("/api/versions/" + S.version.id + "/decide", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ point_label: label, action, reason }),
    });
    status("测点 " + label + " 已执行 " + action);
    renderAll();
  } catch (e) { /* 已提示 */ }
}

/* ---------------------------------------------------------------- 补测路径 */

async function planPath() {
  if (!S.version) return;
  const data = await api("/api/versions/" + S.version.id + "/path");
  pathOverlay = data;
  drawPath(data);
  const ol = $("#pathList");
  ol.innerHTML = "";
  for (const s of data.stops) {
    const li = document.createElement("li");
    li.innerHTML = "<b>#" + s.seq + "</b> (" + s.x + ", " + s.y + ") 覆盖 "
      + s.cells + " 格 <span class='dim'>" + s.statuses.map(k => STATUS_TEXT[k]).join("/") + "</span>";
    li.title = s.reasons.join("\n");
    ol.appendChild(li);
  }
  $("#pathInfo").textContent = data.stops.length
    ? "共 " + data.stops.length + " 站,路径约 " + data.total_m + " m"
    : "当前没有需要补测的区域";
}

function drawPath(data) {
  const g = $("#pathLayer");
  if (!g) return;
  g.innerHTML = "";
  if (!data.stops.length) return;
  el("polyline", { points: data.stops.map(s => s.x + "," + s.y).join(" "),
    fill: "none", stroke: "#ffd75e", "stroke-width": 0.4, "stroke-dasharray": "1.6 1" }, g);
  for (const s of data.stops) {
    el("circle", { cx: s.x, cy: s.y, r: 1.1, fill: "rgba(255,215,94,.25)",
      stroke: "#ffd75e", "stroke-width": 0.3 }, g);
    const t = el("text", { x: s.x, y: s.y + 0.65, "font-size": 1.8, fill: "#ffd75e",
      "text-anchor": "middle" }, g);
    t.textContent = s.seq;
  }
}

/* ---------------------------------------------------------------- 事件绑定 */

document.querySelectorAll(".tool").forEach(b => b.onclick = () => {
  tool = b.dataset.tool;
  document.querySelectorAll(".tool").forEach(x => x.classList.toggle("active", x === b));
  polyDraft = [];
  const drawing = tool === "audience" || tool === "notest";
  $("#polyBar").classList.toggle("hidden", !(drawing || tool === "interference"));
  viewport.classList.toggle("drawing", drawing || tool === "interference");
  const g = $("#draftLayer"); if (g) g.innerHTML = "";
  status(drawing ? "单击添加顶点,双击或“完成”闭合" :
    tool === "interference" ? "在干扰设备位置单击放置(半径 2.5m)" :
    tool === "pan" ? "拖动平移视图" : "点击选择,拖动可移动误定位测点");
});

$("#btnPolyDone").onclick = () => {
  if (tool === "interference") { $("#polyBar").classList.add("hidden"); return; }
  finishPoly();
};
$("#btnPolyCancel").onclick = () => {
  polyDraft = []; $("#polyBar").classList.add("hidden");
  const g = $("#draftLayer"); if (g) g.innerHTML = "";
};

$("#btnImport").onclick = async () => {
  const f = $("#csvFile").files[0];
  if (!f) { status("请先选择 CSV 文件"); return; }
  const fd = new FormData();
  fd.append("csv", f);
  fd.append("label", $("#csvLabel").value || f.name.replace(/\.csv$/i, ""));
  S = await api("/api/projects/" + projectId + "/import", { method: "POST", body: fd });
  status("已生成版本 #" + S.version.id + "「" + S.version.label + "」,仅重算受影响网格");
  pathOverlay = null;
  renderAll();
};

$("#btnSaveLimits").onclick = async () => {
  const data = {};
  document.querySelectorAll("#limitsForm input").forEach(inp => data[inp.dataset.k] = inp.value);
  S = await api("/api/projects/" + projectId + "/limits", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  status("限值已保存,网格已全量重算");
  renderAll();
};

$("#versionSelect").onchange = (ev) => { pathOverlay = null; loadState(+ev.target.value); };
$("#btnPath").onclick = planPath;
$("#btnExportSvg").onclick = () => { if (S.version) location = "/api/versions/" + S.version.id + "/export/coverage.svg"; };
$("#btnExportCsv").onclick = () => { if (S.version) location = "/api/versions/" + S.version.id + "/export/remeasure.csv"; };
$("#btnExportJson").onclick = () => { if (S.version) location = "/api/versions/" + S.version.id + "/export/recalc.json"; };

$("#btnCreate").onclick = async () => {
  const f = $("#setupSvg").files[0];
  if (!f) { alert("请选择场地 SVG"); return; }
  const fd = new FormData();
  fd.append("name", $("#setupName").value);
  fd.append("venue", f);
  const r = await api("/api/projects", { method: "POST", body: fd });
  projectId = r.id;
  $("#setup").classList.add("hidden");
  $("#main").classList.remove("hidden");
  await loadState();
};

boot();
