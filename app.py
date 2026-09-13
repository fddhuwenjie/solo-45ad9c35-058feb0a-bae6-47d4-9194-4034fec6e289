"""助听感应环巡测校审 — Flask 后端。

职责:解析测量 CSV、空间计算(调用 survey.spatial)、SQLite 版本存取、
限值校核、增量重算、补测路径规划与三类导出(覆盖 SVG / 补测清单 / 复算 JSON)。
复测对照:跨轮次配对与差异计算(调用 survey.compare),确认后冻结结果并导出。
边界外逸:开/关工况配对与沿线插值(调用 survey.leakage),人工改配/调边界形成修订,
确认后锁定来源测次、配对表与限值,再导出标色边界 SVG / 复测点 CSV / 复算 JSON。
驱动基准:功放电流记录 + 时钟锚点 -> 分段时码映射 -> 归一化覆盖(调用 survey.drive),
换绑锚点/保留异常段须备注并形成新修订;确认后锁定记录与参数,
复测对照只能引用已确认的驱动版本,导出可追到电流样本与换算值。
"""
import json
import re

from flask import Flask, Response, jsonify, render_template, request

from survey import compare, csvio, drive, leakage, spatial
from survey.db import get_db, init_db, row, rows

app = Flask(__name__)
init_db()

DEFAULT_LIMITS = {
    "field_min": -12.0, "field_max": 0.0, "uniformity_db": 6.0,
    "snr_min": 20.0, "freq_dev_db": 3.0, "ref_freq": 1000.0,
    "expected_freqs": [100, 500, 1000, 2000, 4000, 5000],
    "cell_size": 2.0, "influence_radius": 6.0, "min_points": 3,
}


# ---------------------------------------------------------------- 工具

def parse_svg_bounds(svg_text):
    m = re.search(r'viewBox\s*=\s*"([^"]+)"', svg_text)
    if m:
        parts = [float(v) for v in re.split(r"[,\s]+", m.group(1).strip()) if v]
        if len(parts) == 4:
            return {"min_x": parts[0], "min_y": parts[1], "width": parts[2], "height": parts[3]}
    w = re.search(r'\bwidth\s*=\s*"([\d.]+)', svg_text)
    h = re.search(r'\bheight\s*=\s*"([\d.]+)', svg_text)
    if w and h:
        return {"min_x": 0.0, "min_y": 0.0, "width": float(w.group(1)), "height": float(h.group(1))}
    return {"min_x": 0.0, "min_y": 0.0, "width": 100.0, "height": 100.0}


def svg_inner(svg_text):
    m = re.search(r"<svg[^>]*>(.*)</svg\s*>", svg_text, re.S)
    return m.group(1) if m else svg_text


def get_limits(conn, project_id):
    r = row(conn, "SELECT * FROM limits WHERE project_id=?", (project_id,))
    if not r:
        conn.execute("INSERT INTO limits(project_id) VALUES(?)", (project_id,))
        conn.commit()
        r = row(conn, "SELECT * FROM limits WHERE project_id=?", (project_id,))
    lim = {k: r[k] for k in DEFAULT_LIMITS if k != "expected_freqs"}
    lim["expected_freqs"] = json.loads(r["expected_freqs"])
    return lim


def get_zones(conn, project_id):
    zs = rows(conn, "SELECT * FROM zones WHERE project_id=? ORDER BY id", (project_id,))
    for z in zs:
        z["polygon"] = json.loads(z.pop("polygon_json"))
    return zs


def latest_version(conn, project_id):
    return row(conn, "SELECT * FROM versions WHERE project_id=? ORDER BY id DESC LIMIT 1",
               (project_id,))


def load_points(conn, version_id, limits, bounds):
    meas = rows(conn, "SELECT * FROM measurements WHERE version_id=?", (version_id,))
    return spatial.aggregate_points(meas, limits["expected_freqs"], limits["ref_freq"], bounds)


def calibration_summary(points):
    devs = {}
    for p in points:
        key = (p["device_id"] or "未标注", p["calib_version"] or "未标注")
        devs.setdefault(key, 0)
        devs[key] += 1
    # 混用判定只统计实际参与结论的测点(有效且未排除)
    usable_calibs = {p["calib_version"] or "未标注"
                     for p in points if p["valid"] and not p["excluded"]}
    return {
        "devices": [{"device_id": k[0], "calib_version": k[1], "n_points": n}
                    for k, n in sorted(devs.items())],
        "mixed_calib": len(usable_calibs) > 1,
        "n_points": len(points),
        "n_valid": sum(1 for p in points if p["valid"]),
        "n_excluded": sum(1 for p in points if p["excluded"]),
        "n_locked": sum(1 for p in points if p["locked"]),
    }


def recompute(conn, version_id, affected=None, prev_version_id=None):
    """重算指定版本的网格;affected 为 None 时全量,否则增量(沿用 prev_version 的格子)。"""
    ver = row(conn, "SELECT * FROM versions WHERE id=?", (version_id,))
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (ver["project_id"],))
    bounds = json.loads(proj["bounds_json"])
    limits = get_limits(conn, ver["project_id"])
    zones = get_zones(conn, ver["project_id"])
    points = load_points(conn, version_id, limits, bounds)

    prev_cells = None
    if affected is not None and prev_version_id:
        prev_cells = rows(conn, "SELECT * FROM grid_cells WHERE version_id=?", (prev_version_id,))
        for c in prev_cells:
            c["field"] = c.pop("field_db")
            c["snr"] = c.pop("snr_db")
            c["uniformity"] = c.pop("uniformity_db")
            c["freq_dev"] = c.pop("freq_dev_db")
            c["n"] = c.pop("n_points")
    cells, meta = spatial.compute_grid(points, zones, limits, bounds,
                                       prev_cells=prev_cells, affected=affected)
    conn.execute("DELETE FROM grid_cells WHERE version_id=?", (version_id,))
    conn.executemany(
        "INSERT INTO grid_cells(version_id,cx,cy,x,y,field_db,snr_db,uniformity_db,"
        "freq_dev_db,status,reason,n_points) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [(version_id, c["cx"], c["cy"], c["x"], c["y"], c["field"], c["snr"],
          c["uniformity"], c["freq_dev"], c["status"], c["reason"], c["n"]) for c in cells])
    summary = calibration_summary(points)
    conn.execute("UPDATE versions SET calibration_summary_json=? WHERE id=?",
                 (json.dumps(summary, ensure_ascii=False), version_id))
    conn.commit()
    return points, cells, meta


def log_decision(conn, version_id, kind, payload):
    conn.execute("INSERT INTO decisions(version_id,kind,payload_json) VALUES(?,?,?)",
                 (version_id, kind, json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def build_state(conn, project_id, version_id=None):
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (project_id,))
    if not proj:
        return None
    bounds = json.loads(proj["bounds_json"])
    limits = get_limits(conn, project_id)
    zones = get_zones(conn, project_id)
    versions = rows(conn, "SELECT * FROM versions WHERE project_id=? ORDER BY id DESC",
                    (project_id,))
    for v in versions:
        v["calibration_summary"] = json.loads(v.pop("calibration_summary_json") or "null")
    ver = None
    if version_id:
        ver = next((v for v in versions if v["id"] == version_id), None)
    if ver is None and versions:
        ver = versions[0]

    points, cells, decisions, grid_meta = [], [], [], None
    if ver:
        points = load_points(conn, ver["id"], limits, bounds)
        cells = rows(conn, "SELECT * FROM grid_cells WHERE version_id=?", (ver["id"],))
        for c in cells:
            c["field"] = c.pop("field_db")
            c["snr"] = c.pop("snr_db")
            c["uniformity"] = c.pop("uniformity_db")
            c["freq_dev"] = c.pop("freq_dev_db")
            c["n"] = c.pop("n_points")
        cs = limits["cell_size"]
        grid_meta = {"cs": cs,
                     "nx": max(1, -(-bounds["width"] // cs)),
                     "ny": max(1, -(-bounds["height"] // cs))}
        decisions = rows(conn,
                         "SELECT d.*, v.label AS version_label FROM decisions d "
                         "JOIN versions v ON v.id=d.version_id "
                         "WHERE v.project_id=? ORDER BY d.id DESC LIMIT 100", (project_id,))
        for d in decisions:
            d["payload"] = json.loads(d.pop("payload_json"))
    stats = {}
    for c in cells:
        stats[c["status"]] = stats.get(c["status"], 0) + 1
    return {
        "project": {"id": proj["id"], "name": proj["name"], "bounds": bounds,
                    "venue_svg": proj["venue_svg"]},
        "limits": limits, "zones": zones, "versions": versions, "version": ver,
        "points": points, "cells": cells, "grid": grid_meta, "stats": stats,
        "decisions": decisions,
        "issue_text": spatial.ISSUE_TEXT,
    }


def json_error(msg, code=400):
    return jsonify({"error": msg}), code


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/compare")
def compare_page():
    return render_template("compare.html")


@app.route("/leakage")
def leakage_page():
    return render_template("leakage.html")


@app.route("/drive")
def drive_page():
    return render_template("drive.html")


# ---------------------------------------------------------------- 项目

@app.route("/api/projects", methods=["GET"])
def list_projects():
    conn = get_db()
    ps = rows(conn, "SELECT id,name,created_at FROM projects ORDER BY id DESC")
    conn.close()
    return jsonify(ps)


@app.route("/api/projects", methods=["POST"])
def create_project():
    name = request.form.get("name") or "未命名剧场"
    svg_text = None
    if "venue" in request.files and request.files["venue"].filename:
        svg_text = request.files["venue"].read().decode("utf-8", "replace")
    elif request.form.get("venue_svg"):
        svg_text = request.form["venue_svg"]
    if not svg_text or "<svg" not in svg_text:
        return json_error("需要上传场地 SVG 文件")
    bounds = parse_svg_bounds(svg_text)
    conn = get_db()
    cur = conn.execute("INSERT INTO projects(name,venue_svg,bounds_json) VALUES(?,?,?)",
                       (name, svg_text, json.dumps(bounds)))
    pid = cur.lastrowid
    conn.execute("INSERT INTO limits(project_id) VALUES(?)", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"id": pid, "bounds": bounds})


@app.route("/api/projects/<int:pid>/state")
def project_state(pid):
    conn = get_db()
    vid = request.args.get("version_id", type=int)
    state = build_state(conn, pid, vid)
    conn.close()
    if not state:
        return json_error("项目不存在", 404)
    return jsonify(state)


# ---------------------------------------------------------------- 数据导入

@app.route("/api/projects/<int:pid>/import", methods=["POST"])
def import_csv(pid):
    if "csv" not in request.files or not request.files["csv"].filename:
        return json_error("缺少 CSV 文件")
    text = request.files["csv"].read().decode("utf-8-sig", "replace")
    new_rows, errors = csvio.parse_measurements_csv(text)
    if not new_rows:
        return json_error("CSV 无有效数据行", 400 if not errors else 422)

    conn = get_db()
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (pid,))
    if not proj:
        conn.close()
        return json_error("项目不存在", 404)
    bounds = json.loads(proj["bounds_json"])
    limits = get_limits(conn, pid)
    prev = latest_version(conn, pid)
    label = request.form.get("label") or "巡测 %d" % ((prev["id"] if prev else 0) + 1)

    cur = conn.execute("INSERT INTO versions(project_id,label,source) VALUES(?,?,?)",
                       (pid, label, "csv-import"))
    vid = cur.lastrowid

    # 继承上一版本的全部测量行(含锁定/排除等人工标记)
    if prev:
        conn.execute(
            "INSERT INTO measurements(version_id,point_label,x,y,freq_hz,field_db,noise_db,"
            "device_id,calib_version,locked,excluded,exclude_reason,moved) "
            "SELECT ?,point_label,x,y,freq_hz,field_db,noise_db,device_id,calib_version,"
            "locked,excluded,exclude_reason,moved FROM measurements WHERE version_id=?",
            (vid, prev["id"]))

    # 合并新数据:同名测点视为复测并替换;已锁定测点拒绝覆盖
    replaced, skipped_locked, added = [], [], []
    by_label = {}
    for r in new_rows:
        by_label.setdefault(r["point_label"], []).append(r)
    for plabel, rs in by_label.items():
        old = rows(conn, "SELECT * FROM measurements WHERE version_id=? AND point_label=?",
                   (vid, plabel))
        if old and max(o["locked"] for o in old):
            skipped_locked.append(plabel)
            continue
        if old:
            replaced.append(plabel)
            conn.execute("DELETE FROM measurements WHERE version_id=? AND point_label=?",
                         (vid, plabel))
        else:
            added.append(plabel)
        conn.executemany(
            "INSERT INTO measurements(version_id,point_label,x,y,freq_hz,field_db,noise_db,"
            "device_id,calib_version) VALUES(?,?,?,?,?,?,?,?,?)",
            [(vid, r["point_label"], r["x"], r["y"], r["freq_hz"], r["field_db"],
              r["noise_db"], r["device_id"], r["calib_version"]) for r in rs])

    # 增量重算:受影响位置 = 新增/替换点新位置 + 被替换点旧位置
    locations = [(r["x"], r["y"]) for r in new_rows if r["point_label"] not in skipped_locked]
    if prev:
        old_pts = rows(conn,
                       "SELECT DISTINCT x,y FROM measurements WHERE version_id=? "
                       "AND point_label IN (%s)" % ",".join("?" * len(replaced)),
                       (prev["id"], *replaced)) if replaced else []
        locations += [(o["x"], o["y"]) for o in old_pts]
    affected = spatial.affected_cells(locations, limits, bounds) if locations else set()
    recompute(conn, vid, affected=affected if prev else None,
              prev_version_id=prev["id"] if prev else None)

    log_decision(conn, vid, "import", {
        "label": label, "rows": len(new_rows), "added": added,
        "replaced": replaced, "skipped_locked": skipped_locked, "csv_errors": errors})
    state = build_state(conn, pid, vid)
    conn.close()
    state["import_result"] = {"added": added, "replaced": replaced,
                              "skipped_locked": skipped_locked, "csv_errors": errors}
    return jsonify(state)


# ---------------------------------------------------------------- 限值 / 分区

@app.route("/api/projects/<int:pid>/limits", methods=["PUT"])
def update_limits(pid):
    data = request.get_json(force=True)
    conn = get_db()
    get_limits(conn, pid)
    freqs = data.get("expected_freqs", DEFAULT_LIMITS["expected_freqs"])
    if isinstance(freqs, str):
        freqs = [float(x) for x in re.split(r"[,\s]+", freqs.strip()) if x]
    freqs = sorted(float(f) for f in freqs)
    conn.execute(
        "UPDATE limits SET field_min=?,field_max=?,uniformity_db=?,snr_min=?,freq_dev_db=?,"
        "ref_freq=?,expected_freqs=?,cell_size=?,influence_radius=?,min_points=? "
        "WHERE project_id=?",
        (float(data["field_min"]), float(data["field_max"]), float(data["uniformity_db"]),
         float(data["snr_min"]), float(data["freq_dev_db"]), float(data["ref_freq"]),
         json.dumps(freqs), float(data["cell_size"]), float(data["influence_radius"]),
         int(data["min_points"]), pid))
    conn.commit()
    ver = latest_version(conn, pid)
    if ver:
        recompute(conn, ver["id"])  # 限值变化 -> 全量重算
    state = build_state(conn, pid)
    conn.close()
    return jsonify(state)


@app.route("/api/projects/<int:pid>/zones", methods=["POST"])
def add_zone(pid):
    data = request.get_json(force=True)
    kind = data.get("kind")
    poly = data.get("polygon")
    if kind not in ("audience", "notest", "interference"):
        return json_error("kind 必须为 audience/notest/interference")
    if not isinstance(poly, list) or len(poly) < 3:
        return json_error("多边形至少需要 3 个顶点")
    conn = get_db()
    conn.execute("INSERT INTO zones(project_id,kind,name,polygon_json) VALUES(?,?,?,?)",
                 (pid, kind, data.get("name", ""), json.dumps(poly)))
    conn.commit()
    ver = latest_version(conn, pid)
    if ver:
        recompute(conn, ver["id"])  # 分区变化 -> 全量重算
    state = build_state(conn, pid)
    conn.close()
    return jsonify(state)


@app.route("/api/zones/<int:zid>", methods=["DELETE"])
def delete_zone(zid):
    conn = get_db()
    z = row(conn, "SELECT * FROM zones WHERE id=?", (zid,))
    if not z:
        conn.close()
        return json_error("分区不存在", 404)
    conn.execute("DELETE FROM zones WHERE id=?", (zid,))
    conn.commit()
    ver = latest_version(conn, z["project_id"])
    if ver:
        recompute(conn, ver["id"])
    state = build_state(conn, z["project_id"])
    conn.close()
    return jsonify(state)


# ---------------------------------------------------------------- 测点人工操作

@app.route("/api/versions/<int:vid>/move", methods=["POST"])
def move_point(vid):
    data = request.get_json(force=True)
    label = data.get("point_label")
    x, y = float(data["x"]), float(data["y"])
    conn = get_db()
    ver = row(conn, "SELECT * FROM versions WHERE id=?", (vid,))
    if not ver:
        conn.close()
        return json_error("版本不存在", 404)
    meas = rows(conn, "SELECT * FROM measurements WHERE version_id=? AND point_label=?",
                (vid, label))
    if not meas:
        conn.close()
        return json_error("测点不存在", 404)
    if max(m["locked"] for m in meas):
        conn.close()
        return json_error("测点已锁定(已复核),不可移动", 409)
    old = (meas[0]["x"], meas[0]["y"])
    conn.execute("UPDATE measurements SET x=?,y=?,moved=1 WHERE version_id=? AND point_label=?",
                 (x, y, vid, label))
    conn.commit()
    log_decision(conn, vid, "move", {"point_label": label, "from": old, "to": [x, y]})
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (ver["project_id"],))
    bounds = json.loads(proj["bounds_json"])
    limits = get_limits(conn, ver["project_id"])
    affected = spatial.affected_cells([old, (x, y)], limits, bounds)
    recompute(conn, vid, affected=affected, prev_version_id=vid)
    state = build_state(conn, ver["project_id"], vid)
    conn.close()
    return jsonify(state)


@app.route("/api/versions/<int:vid>/decide", methods=["POST"])
def decide(vid):
    """人工决定:lock/unlock(复核锁定)、exclude(填写排除理由)/include。"""
    data = request.get_json(force=True)
    label = data.get("point_label")
    action = data.get("action")
    reason = (data.get("reason") or "").strip()
    conn = get_db()
    ver = row(conn, "SELECT * FROM versions WHERE id=?", (vid,))
    meas = rows(conn, "SELECT * FROM measurements WHERE version_id=? AND point_label=?",
                (vid, label))
    if not ver or not meas:
        conn.close()
        return json_error("版本或测点不存在", 404)
    locked = max(m["locked"] for m in meas)
    if action in ("exclude", "move") and locked:
        conn.close()
        return json_error("测点已锁定,不可排除", 409)
    if action == "lock":
        conn.execute("UPDATE measurements SET locked=1 WHERE version_id=? AND point_label=?",
                     (vid, label))
    elif action == "unlock":
        conn.execute("UPDATE measurements SET locked=0 WHERE version_id=? AND point_label=?",
                     (vid, label))
    elif action == "exclude":
        if not reason:
            conn.close()
            return json_error("排除必须填写理由(如:临时异常——调光设备测试)")
        conn.execute(
            "UPDATE measurements SET excluded=1,exclude_reason=? "
            "WHERE version_id=? AND point_label=?", (reason, vid, label))
    elif action == "include":
        conn.execute(
            "UPDATE measurements SET excluded=0,exclude_reason='' "
            "WHERE version_id=? AND point_label=?", (vid, label))
    else:
        conn.close()
        return json_error("未知操作")
    conn.commit()
    log_decision(conn, vid, action, {"point_label": label, "reason": reason})

    if action in ("exclude", "include"):
        proj = row(conn, "SELECT * FROM projects WHERE id=?", (ver["project_id"],))
        bounds = json.loads(proj["bounds_json"])
        limits = get_limits(conn, ver["project_id"])
        loc = (meas[0]["x"], meas[0]["y"])
        affected = spatial.affected_cells([loc], limits, bounds)
        recompute(conn, vid, affected=affected, prev_version_id=vid)
    state = build_state(conn, ver["project_id"], vid)
    conn.close()
    return jsonify(state)


# ---------------------------------------------------------------- 补测路径

def _path_for(conn, vid):
    ver = row(conn, "SELECT * FROM versions WHERE id=?", (vid,))
    if not ver:
        return None, None, None
    cells = rows(conn, "SELECT * FROM grid_cells WHERE version_id=?", (vid,))
    stops = spatial.remeasure_stops(cells)
    order, total = spatial.tsp_order(stops)
    return ver, order, total


@app.route("/api/versions/<int:vid>/path")
def remeasure_path(vid):
    conn = get_db()
    ver, order, total = _path_for(conn, vid)
    conn.close()
    if ver is None:
        return json_error("版本不存在", 404)
    return jsonify({"version_id": vid, "stops": order, "total_m": total})


# ---------------------------------------------------------------- 导出

STATUS_COLOR = {"ok": "#2e9e5b", "warn": "#d8a012", "fail": "#d24040",
                "nodata": "#8a8f98", "noconclusion": "#9b59b6", "notest": "#3a3f47"}
STATUS_TEXT = {"ok": "合格", "warn": "临近干扰", "fail": "不合格",
               "nodata": "证据不足", "noconclusion": "不作结论", "notest": "禁测区"}


@app.route("/api/versions/<int:vid>/export/coverage.svg")
def export_svg(vid):
    conn = get_db()
    ver = row(conn, "SELECT * FROM versions WHERE id=?", (vid,))
    if not ver:
        conn.close()
        return json_error("版本不存在", 404)
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (ver["project_id"],))
    bounds = json.loads(proj["bounds_json"])
    limits = get_limits(conn, ver["project_id"])
    zones = get_zones(conn, ver["project_id"])
    points = load_points(conn, vid, limits, bounds)
    cells = rows(conn, "SELECT * FROM grid_cells WHERE version_id=?", (vid,))
    cs = limits["cell_size"]

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.2f %.2f %.2f %.2f" '
             'font-family="sans-serif">' % (bounds["min_x"] - 4, bounds["min_y"] - 4,
                                            bounds["width"] + 8, bounds["height"] + 10)]
    parts.append("<g opacity='0.35'>%s</g>" % svg_inner(proj["venue_svg"]))
    for c in cells:
        parts.append(
            "<rect x='%.2f' y='%.2f' width='%.2f' height='%.2f' fill='%s' fill-opacity='0.55'>"
            "<title>%s %s</title></rect>"
            % (c["x"] - cs / 2, c["y"] - cs / 2, cs, cs,
               STATUS_COLOR.get(c["status"], "#888"), c["status"], c["reason"]))
    zone_style = {"audience": ("#2e9e5b", "无"), "notest": ("#666", "斜线"),
                  "interference": ("#d8a012", "干扰")}
    for z in zones:
        pts = " ".join("%.2f,%.2f" % (p[0], p[1]) for p in z["polygon"])
        color = zone_style[z["kind"]][0]
        parts.append("<polygon points='%s' fill='none' stroke='%s' stroke-width='0.35' "
                     "stroke-dasharray='1.2 0.8'><title>%s:%s</title></polygon>"
                     % (pts, color, z["kind"], z["name"]))
    for p in points:
        color = "#888" if p["excluded"] else ("#d24040" if not p["valid"] else "#1c6dd9")
        parts.append("<circle cx='%.2f' cy='%.2f' r='0.55' fill='%s' stroke='#fff' "
                     "stroke-width='0.15'><title>%s</title></circle>"
                     % (p["x"], p["y"], color, p["label"]))
        if p["locked"]:
            parts.append("<circle cx='%.2f' cy='%.2f' r='0.9' fill='none' stroke='#fff' "
                         "stroke-width='0.25'/>" % (p["x"], p["y"]))
    lx, ly = bounds["min_x"], bounds["min_y"] + bounds["height"] + 2
    for i, (st, txt) in enumerate(STATUS_TEXT.items()):
        parts.append("<rect x='%.2f' y='%.2f' width='2' height='2' fill='%s'/>"
                     "<text x='%.2f' y='%.2f' font-size='2.2' fill='#222'>%s</text>"
                     % (lx + i * 14, ly, STATUS_COLOR[st], lx + i * 14 + 2.6, ly + 1.8, txt))
    parts.append("<text x='%.2f' y='%.2f' font-size='2.2' fill='#222'>%s · 版本 %s</text>"
                 % (bounds["min_x"], bounds["min_y"] - 1.5, proj["name"], ver["label"]))
    parts.append("</svg>")
    conn.close()
    return Response("".join(parts), mimetype="image/svg+xml", headers={
        "Content-Disposition": "attachment; filename=coverage_v%s.svg" % vid})


@app.route("/api/versions/<int:vid>/export/remeasure.csv")
def export_remeasure(vid):
    conn = get_db()
    ver, order, total = _path_for(conn, vid)
    conn.close()
    if ver is None:
        return json_error("版本不存在", 404)
    lines = ["seq,x,y,cells,statuses,reasons"]
    for s in order:
        lines.append("%d,%.2f,%.2f,%d,%s,\"%s\""
                     % (s["seq"], s["x"], s["y"], s["cells"],
                        "|".join(s["statuses"]), ";".join(s["reasons"])))
    lines.append("# total_path_m,%.2f" % total)
    return Response("﻿" + "\n".join(lines), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=remeasure_v%s.csv" % vid})


@app.route("/api/versions/<int:vid>/export/recalc.json")
def export_recalc(vid):
    conn = get_db()
    ver = row(conn, "SELECT * FROM versions WHERE id=?", (vid,))
    if not ver:
        conn.close()
        return json_error("版本不存在", 404)
    state = build_state(conn, ver["project_id"], vid)
    _, order, total = _path_for(conn, vid)
    # manual_decisions 按版本隔离:只导出本版产生的决定,不带入其他版本
    decisions = rows(conn,
                     "SELECT * FROM decisions WHERE version_id=? ORDER BY id", (vid,))
    for d in decisions:
        d["payload"] = json.loads(d.pop("payload_json"))
    conn.close()
    payload = {
        "project": {"id": state["project"]["id"], "name": state["project"]["name"],
                    "bounds": state["project"]["bounds"]},
        "version": {"id": ver["id"], "label": ver["label"],
                    "created_at": ver["created_at"]},
        "limits": state["limits"],
        "calibration_summary": state["version"]["calibration_summary"],
        "manual_decisions": decisions,
        "validation_issues": [
            {"point_label": p["label"], "issues": p["issues"]}
            for p in state["points"] if not p["valid"]],
        "excluded_points": [
            {"point_label": p["label"], "reason": p["exclude_reason"]}
            for p in state["points"] if p["excluded"]],
        "stats": state["stats"],
        "remeasure_path": {"stops": order, "total_m": total},
        "cells": state["cells"],
    }
    return Response(json.dumps(payload, ensure_ascii=False, indent=1),
                    mimetype="application/json", headers={
                        "Content-Disposition": "attachment; filename=recalc_v%s.json" % vid})


# ---------------------------------------------------------------- 复测对照

CONDITION_FIELDS = ("base_occ", "base_lighting", "base_pa",
                    "retest_occ", "retest_lighting", "retest_pa")


def log_c_event(conn, cid, kind, payload):
    conn.execute("INSERT INTO comparison_events(comparison_id,kind,payload_json) "
                 "VALUES(?,?,?)", (cid, kind, json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def recompute_comparison(conn, cid):
    """按当前配对表(自动 + 人工改配)重算对照结果并写入 result_json。"""
    comp = row(conn, "SELECT * FROM comparisons WHERE id=?", (cid,))
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (comp["project_id"],))
    bounds = json.loads(proj["bounds_json"])
    limits = get_limits(conn, comp["project_id"])
    base_pts = load_points(conn, comp["base_version_id"], limits, bounds)
    retest_pts = load_points(conn, comp["retest_version_id"], limits, bounds)
    overrides = rows(conn, "SELECT base_label,retest_label,note FROM pair_overrides "
                           "WHERE comparison_id=? ORDER BY id", (cid,))
    result = compare.compute_comparison(base_pts, retest_pts, limits, bounds,
                                        pos_tol=comp["pos_tol"], overrides=overrides)
    conn.execute("UPDATE comparisons SET result_json=? WHERE id=?",
                 (json.dumps(result, ensure_ascii=False), cid))
    conn.commit()


def comparison_state(conn, cid):
    comp = row(conn, "SELECT * FROM comparisons WHERE id=?", (cid,))
    if not comp:
        return None
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (comp["project_id"],))
    vers = {v["id"]: v for v in rows(
        conn, "SELECT id,label,created_at FROM versions WHERE project_id=? ORDER BY id",
        (comp["project_id"],))}
    max_vid = max(vers) if vers else 0
    stale = bool(comp["status"] == "confirmed"
                 and comp["source_max_version_id"] is not None
                 and max_vid > comp["source_max_version_id"])
    overrides = rows(conn, "SELECT * FROM pair_overrides WHERE comparison_id=? "
                           "ORDER BY id", (cid,))
    events = rows(conn, "SELECT * FROM comparison_events WHERE comparison_id=? "
                        "ORDER BY id DESC LIMIT 50", (cid,))
    for e in events:
        e["payload"] = json.loads(e.pop("payload_json"))
    return {
        "id": comp["id"], "project_id": comp["project_id"],
        "project_name": proj["name"],
        "label": comp["label"], "status": comp["status"],
        "conditions": {k: comp[k] for k in CONDITION_FIELDS},
        "pos_tol": comp["pos_tol"],
        "base_version": vers.get(comp["base_version_id"]),
        "retest_version": vers.get(comp["retest_version_id"]),
        "base_version_id": comp["base_version_id"],
        "retest_version_id": comp["retest_version_id"],
        "created_at": comp["created_at"], "confirmed_at": comp["confirmed_at"],
        "stale": stale,
        "result": json.loads(comp["result_json"]) if comp["result_json"] else None,
        "overrides": overrides, "events": events,
        "migration_text": compare.MIGRATION_TEXT,
        "nc_text": compare.NC_TEXT,
        "project_bounds": json.loads(proj["bounds_json"]),
        "venue_svg": proj["venue_svg"],
    }


def get_comparison(conn, cid):
    return row(conn, "SELECT * FROM comparisons WHERE id=?", (cid,))


@app.route("/api/projects/<int:pid>/comparisons", methods=["GET"])
def list_comparisons(pid):
    conn = get_db()
    comps = rows(conn, "SELECT * FROM comparisons WHERE project_id=? ORDER BY id DESC",
                 (pid,))
    max_vid = row(conn, "SELECT MAX(id) AS m FROM versions WHERE project_id=?",
                  (pid,))["m"] or 0
    vers = {v["id"]: v["label"] for v in rows(
        conn, "SELECT id,label FROM versions WHERE project_id=?", (pid,))}
    out = []
    for c in comps:
        result = json.loads(c["result_json"]) if c["result_json"] else None
        out.append({
            "id": c["id"], "label": c["label"], "status": c["status"],
            "base_version_id": c["base_version_id"],
            "retest_version_id": c["retest_version_id"],
            "base_label": vers.get(c["base_version_id"], "?"),
            "retest_label": vers.get(c["retest_version_id"], "?"),
            "created_at": c["created_at"], "confirmed_at": c["confirmed_at"],
            "stale": bool(c["status"] == "confirmed"
                          and c["source_max_version_id"] is not None
                          and max_vid > c["source_max_version_id"]),
            "stats": result["stats"] if result else None,
        })
    conn.close()
    return jsonify(out)


@app.route("/api/projects/<int:pid>/comparisons", methods=["POST"])
def create_comparison(pid):
    data = request.get_json(force=True)
    try:
        base_vid, retest_vid = int(data["base_version_id"]), int(data["retest_version_id"])
    except (KeyError, TypeError, ValueError):
        return json_error("必须指定基准轮次与复测轮次")
    if base_vid == retest_vid:
        return json_error("基准轮次与复测轮次不能相同")
    conn = get_db()
    vers = {v["id"] for v in rows(
        conn, "SELECT id FROM versions WHERE project_id=?", (pid,))}
    if base_vid not in vers or retest_vid not in vers:
        conn.close()
        return json_error("所选轮次不属于本项目", 404)
    cond = {k: (data.get(k) or "").strip() for k in CONDITION_FIELDS}
    pos_tol = float(data.get("pos_tol") or 1.0)
    if pos_tol <= 0:
        conn.close()
        return json_error("位置容差必须为正")
    cur = conn.execute(
        "INSERT INTO comparisons(project_id,base_version_id,retest_version_id,label,"
        "base_occ,base_lighting,base_pa,retest_occ,retest_lighting,retest_pa,pos_tol) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, base_vid, retest_vid,
         (data.get("label") or "").strip()
         or "对照 #%s→#%s" % (base_vid, retest_vid),
         cond["base_occ"], cond["base_lighting"], cond["base_pa"],
         cond["retest_occ"], cond["retest_lighting"], cond["retest_pa"], pos_tol))
    cid = cur.lastrowid
    conn.commit()
    recompute_comparison(conn, cid)
    log_c_event(conn, cid, "create", {
        "base_version_id": base_vid, "retest_version_id": retest_vid,
        "pos_tol": pos_tol, "conditions": cond})
    state = comparison_state(conn, cid)
    conn.close()
    return jsonify(state)


@app.route("/api/comparisons/<int:cid>")
def get_comparison_state(cid):
    conn = get_db()
    state = comparison_state(conn, cid)
    conn.close()
    if not state:
        return json_error("对照不存在", 404)
    return jsonify(state)


@app.route("/api/comparisons/<int:cid>/conditions", methods=["PUT"])
def update_conditions(cid):
    data = request.get_json(force=True)
    conn = get_db()
    comp = get_comparison(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照不存在", 404)
    if comp["status"] == "confirmed":
        conn.close()
        return json_error("对照已确认,工况与配对参数已锁定;如需修改请先重审", 409)
    cond = {k: (data.get(k) or "").strip() for k in CONDITION_FIELDS}
    pos_tol = float(data.get("pos_tol") or comp["pos_tol"])
    if pos_tol <= 0:
        conn.close()
        return json_error("位置容差必须为正")
    label = (data.get("label") or comp["label"]).strip()
    conn.execute(
        "UPDATE comparisons SET label=?,base_occ=?,base_lighting=?,base_pa=?,"
        "retest_occ=?,retest_lighting=?,retest_pa=?,pos_tol=? WHERE id=?",
        (label, cond["base_occ"], cond["base_lighting"], cond["base_pa"],
         cond["retest_occ"], cond["retest_lighting"], cond["retest_pa"], pos_tol, cid))
    conn.commit()
    recompute_comparison(conn, cid)
    log_c_event(conn, cid, "conditions", {"label": label, "pos_tol": pos_tol,
                                          "conditions": cond})
    state = comparison_state(conn, cid)
    conn.close()
    return jsonify(state)


@app.route("/api/comparisons/<int:cid>/overrides", methods=["POST"])
def add_override(cid):
    """人工改配:强制配对/取消配对,必须备注理由。"""
    data = request.get_json(force=True)
    base_label = (data.get("base_label") or "").strip()
    retest_label = (data.get("retest_label") or "").strip() or None
    note = (data.get("note") or "").strip()
    conn = get_db()
    comp = get_comparison(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照不存在", 404)
    if comp["status"] == "confirmed":
        conn.close()
        return json_error("对照已确认,配对表已锁定;如需改配请先重审", 409)
    if not base_label:
        conn.close()
        return json_error("缺少基准测点")
    if not note:
        conn.close()
        return json_error("人工改配必须备注理由", 422)
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (comp["project_id"],))
    bounds = json.loads(proj["bounds_json"])
    limits = get_limits(conn, comp["project_id"])
    base_labels = {p["label"] for p in load_points(conn, comp["base_version_id"], limits, bounds)}
    retest_labels = {p["label"] for p in load_points(conn, comp["retest_version_id"], limits, bounds)}
    if base_label not in base_labels:
        conn.close()
        return json_error("基准轮无测点 " + base_label, 404)
    if retest_label and retest_label not in retest_labels:
        conn.close()
        return json_error("复测轮无测点 " + retest_label, 404)
    conn.execute("INSERT INTO pair_overrides(comparison_id,base_label,retest_label,note) "
                 "VALUES(?,?,?,?)", (cid, base_label, retest_label, note))
    conn.commit()
    recompute_comparison(conn, cid)
    log_c_event(conn, cid, "override", {
        "base_label": base_label, "retest_label": retest_label, "note": note})
    state = comparison_state(conn, cid)
    conn.close()
    return jsonify(state)


@app.route("/api/comparisons/<int:cid>/overrides/<int:oid>", methods=["DELETE"])
def delete_override(cid, oid):
    conn = get_db()
    comp = get_comparison(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照不存在", 404)
    if comp["status"] == "confirmed":
        conn.close()
        return json_error("对照已确认,配对表已锁定;如需改配请先重审", 409)
    ov = row(conn, "SELECT * FROM pair_overrides WHERE id=? AND comparison_id=?",
             (oid, cid))
    if not ov:
        conn.close()
        return json_error("改配记录不存在", 404)
    conn.execute("DELETE FROM pair_overrides WHERE id=?", (oid,))
    conn.commit()
    recompute_comparison(conn, cid)
    log_c_event(conn, cid, "override-delete", {
        "base_label": ov["base_label"], "retest_label": ov["retest_label"]})
    state = comparison_state(conn, cid)
    conn.close()
    return jsonify(state)


@app.route("/api/comparisons/<int:cid>/confirm", methods=["POST"])
def confirm_comparison(cid):
    """确认:冻结结果,锁定来源轮次与配对表;此后来源派生新修订将提示重审。"""
    conn = get_db()
    comp = get_comparison(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照不存在", 404)
    if comp["status"] == "confirmed":
        conn.close()
        return json_error("对照已确认", 409)
    max_vid = row(conn, "SELECT MAX(id) AS m FROM versions WHERE project_id=?",
                  (comp["project_id"],))["m"] or 0
    conn.execute("UPDATE comparisons SET status='confirmed',"
                 "confirmed_at=datetime('now'),source_max_version_id=? WHERE id=?",
                 (max_vid, cid))
    conn.commit()
    log_c_event(conn, cid, "confirm", {"source_max_version_id": max_vid})
    state = comparison_state(conn, cid)
    conn.close()
    return jsonify(state)


@app.route("/api/comparisons/<int:cid>/reopen", methods=["POST"])
def reopen_comparison(cid):
    """重审:解除锁定回到草稿,可改配/调参后重新确认。"""
    data = request.get_json(force=True) if request.data else {}
    conn = get_db()
    comp = get_comparison(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照不存在", 404)
    if comp["status"] != "confirmed":
        conn.close()
        return json_error("对照尚未确认", 409)
    conn.execute("UPDATE comparisons SET status='draft',confirmed_at=NULL WHERE id=?",
                 (cid,))
    conn.commit()
    log_c_event(conn, cid, "reopen", {"reason": (data.get("reason") or "").strip()})
    state = comparison_state(conn, cid)
    conn.close()
    return jsonify(state)


@app.route("/api/comparisons/<int:cid>/raw")
def comparison_raw(cid):
    """反查两轮原始记录:?labels=P01,P02(如成片退化席位区的成员)。"""
    labels = [s for s in (request.args.get("labels") or "").split(",") if s.strip()]
    if not labels:
        return json_error("缺少 labels 参数")
    conn = get_db()
    comp = get_comparison(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照不存在", 404)
    q = ",".join("?" * len(labels))

    def fetch(vid):
        return rows(conn, "SELECT point_label,x,y,freq_hz,field_db,noise_db,"
                          "device_id,calib_version,locked,excluded,exclude_reason "
                          "FROM measurements WHERE version_id=? AND point_label IN (%s) "
                          "ORDER BY point_label,freq_hz" % q, (vid, *labels))
    out = {
        "base_version_id": comp["base_version_id"],
        "retest_version_id": comp["retest_version_id"],
        "base": fetch(comp["base_version_id"]),
        "retest": fetch(comp["retest_version_id"]),
    }
    conn.close()
    return jsonify(out)


# ---------------------------------------------------------------- 复测对照导出(均取自同一确认结果)

MIGRATION_COLOR = {"ok->fail": "#d24040", "fail->ok": "#2e9e5b",
                   "ok->ok": "#1c6dd9", "fail->fail": "#d8a012"}


def confirmed_result(conn, cid):
    """读取已确认对照的冻结结果;未确认返回 None。"""
    comp = get_comparison(conn, cid)
    if not comp or comp["status"] != "confirmed" or not comp["result_json"]:
        return None, None
    return comp, json.loads(comp["result_json"])


@app.route("/api/comparisons/<int:cid>/export/diff.svg")
def export_diff_svg(cid):
    conn = get_db()
    comp, result = confirmed_result(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照结果尚未确认,三份导出材料必须取自同一确认结果", 409)
    state = comparison_state(conn, cid)
    conn.close()
    bounds = state["project_bounds"]
    cond = state["conditions"]

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.2f %.2f %.2f %.2f" '
             'font-family="sans-serif">' % (bounds["min_x"] - 4, bounds["min_y"] - 6,
                                            bounds["width"] + 8, bounds["height"] + 14)]
    parts.append("<g opacity='0.35'>%s</g>" % svg_inner(state["venue_svg"]))
    for cl in result["clusters"]:
        if not cl["clustered"]:
            continue
        parts.append("<circle cx='%.2f' cy='%.2f' r='%.2f' fill='rgba(210,64,64,.10)' "
                     "stroke='#d24040' stroke-width='0.3' stroke-dasharray='1.4 0.9'>"
                     "<title>成片退化席位区 #%d:%d 个位置</title></circle>"
                     % (cl["x"], cl["y"], 2.0 + 1.2 * cl["n"], cl["id"], cl["n"]))
    for p in result["pairs"]:
        if p["status"] == "noconclusion":
            color = "#9b59b6"
        else:
            color = MIGRATION_COLOR.get(p["migration"]["overall"], "#888")
        if p.get("dist") and p["dist"] > 0.3:
            parts.append("<line x1='%.2f' y1='%.2f' x2='%.2f' y2='%.2f' stroke='%s' "
                         "stroke-width='0.2' stroke-dasharray='0.6 0.5'/>"
                         % (p["x"], p["y"], p["rx"], p["ry"], color))
        parts.append("<circle cx='%.2f' cy='%.2f' r='0.6' fill='%s' stroke='#fff' "
                     "stroke-width='0.15'><title>%s→%s %s</title></circle>"
                     % (p["x"], p["y"], color, p["base_label"], p["retest_label"],
                        p["reason"] or compare.MIGRATION_TEXT.get(
                            (p["migration"] or {}).get("overall"), "")))
        if p["method"] == "manual":
            parts.append("<circle cx='%.2f' cy='%.2f' r='1.0' fill='none' "
                         "stroke='#ffd75e' stroke-width='0.25'/>" % (p["x"], p["y"]))
    legend = [("#d24040", "退化"), ("#2e9e5b", "改善"), ("#1c6dd9", "保持合格"),
              ("#d8a012", "保持不合格"), ("#9b59b6", "无结论")]
    lx, ly = bounds["min_x"], bounds["min_y"] + bounds["height"] + 2
    for i, (color, txt) in enumerate(legend):
        parts.append("<rect x='%.2f' y='%.2f' width='2' height='2' fill='%s'/>"
                     "<text x='%.2f' y='%.2f' font-size='2.2' fill='#222'>%s</text>"
                     % (lx + i * 14, ly, color, lx + i * 14 + 2.6, ly + 1.8, txt))
    cond_txt = "基准[%s/%s/%s] 复测[%s/%s/%s]" % (
        cond["base_occ"] or "—", cond["base_lighting"] or "—", cond["base_pa"] or "—",
        cond["retest_occ"] or "—", cond["retest_lighting"] or "—", cond["retest_pa"] or "—")
    parts.append("<text x='%.2f' y='%.2f' font-size='2.2' fill='#222'>%s · %s · 基准 #%s %s / 复测 #%s %s</text>"
                 % (bounds["min_x"], bounds["min_y"] - 3.2, state["project_name"],
                    state["label"], comp["base_version_id"],
                    (state["base_version"] or {}).get("label", ""),
                    comp["retest_version_id"],
                    (state["retest_version"] or {}).get("label", "")))
    parts.append("<text x='%.2f' y='%.2f' font-size='2.0' fill='#222'>工况(客席/灯光/扩声)%s%s</text>"
                 % (bounds["min_x"], bounds["min_y"] - 0.8, cond_txt,
                    " · ⚠来源已派生新修订,待重审" if state["stale"] else ""))
    parts.append("</svg>")
    return Response("".join(parts), mimetype="image/svg+xml", headers={
        "Content-Disposition": "attachment; filename=diff_c%s.svg" % cid})


@app.route("/api/comparisons/<int:cid>/export/detail.csv")
def export_compare_csv(cid):
    conn = get_db()
    comp, result = confirmed_result(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照结果尚未确认,三份导出材料必须取自同一确认结果", 409)
    conn.close()
    head = ["pair_key", "base_label", "retest_label", "x", "y", "method", "status",
            "reason", "freq_hz", "base_field_db", "retest_field_db", "delta_field_db",
            "base_noise_db", "retest_noise_db", "delta_noise_db",
            "base_field_margin_db", "retest_field_margin_db", "delta_field_margin_db",
            "base_snr_margin_db", "retest_snr_margin_db", "delta_snr_margin_db",
            "migration_overall", "manual_note"]
    lines = [",".join(head)]

    def q(s):
        return '"%s"' % str(s).replace('"', '""') if s else ""

    for p in result["pairs"]:
        common = [p["key"], p["base_label"], p["retest_label"],
                  "%.2f" % p["x"], "%.2f" % p["y"], p["method"], p["status"],
                  q(p["reason"])]
        if p["status"] == "noconclusion":
            lines.append(",".join(common + [""] * 13 + ["", q(p["manual_note"])]))
            continue
        mig = p["migration"]["overall"]
        for f in p["freqs"]:
            lines.append(",".join(common + [
                "%g" % f["freq"], "%.2f" % f["base_field"], "%.2f" % f["retest_field"],
                "%.2f" % f["delta_field"], "%.2f" % f["base_noise"],
                "%.2f" % f["retest_noise"], "%.2f" % f["delta_noise"],
                "%.2f" % f["base_field_margin"], "%.2f" % f["retest_field_margin"],
                "%.2f" % f["delta_field_margin"], "%.2f" % f["base_snr_margin"],
                "%.2f" % f["retest_snr_margin"], "%.2f" % f["delta_snr_margin"],
                mig, q(p["manual_note"])]))
    for a in result["ambiguous"]:
        lines.append(",".join([a["label"], a["label"], "", "", "", "", "noconclusion",
                               q("多重匹配,候选:" + "/".join(a["candidates"]))]
                              + [""] * 15))
    for side, labels in (("base_only", result["unpaired"]["base_only"]),
                         ("retest_only", result["unpaired"]["retest_only"])):
        for lb in labels:
            lines.append(",".join([lb, lb if side == "base_only" else "",
                                   lb if side == "retest_only" else "",
                                   "", "", "", "unpaired", q(side)] + [""] * 15))
    return Response("﻿" + "\n".join(lines), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=compare_detail_c%s.csv" % cid})


@app.route("/api/comparisons/<int:cid>/export/recalc.json")
def export_compare_json(cid):
    conn = get_db()
    comp, result = confirmed_result(conn, cid)
    if not comp:
        conn.close()
        return json_error("对照结果尚未确认,三份导出材料必须取自同一确认结果", 409)
    state = comparison_state(conn, cid)
    conn.close()
    payload = {
        "comparison": {k: state[k] for k in
                       ("id", "project_id", "project_name", "label", "status",
                        "base_version_id", "retest_version_id", "pos_tol",
                        "created_at", "confirmed_at", "stale")},
        "base_version": state["base_version"],
        "retest_version": state["retest_version"],
        "conditions": state["conditions"],
        "stale_notice": "来源轮次在确认后派生了新修订,结论需重审" if state["stale"] else "",
        "overrides": state["overrides"],
        "events": state["events"],
        "migration_text": state["migration_text"],
        "result": result,
    }
    return Response(json.dumps(payload, ensure_ascii=False, indent=1),
                    mimetype="application/json", headers={
                        "Content-Disposition": "attachment; filename=compare_recalc_c%s.json" % cid})


# ---------------------------------------------------------------- 边界外逸

LEAK_PARAM_FIELDS = ("leak_limit_db", "max_field_db", "min_points",
                     "influence_radius", "max_sample_gap", "max_time_gap_h")
LEAK_DEFAULTS = {"leak_limit_db": 6.0, "max_field_db": -32.0, "min_points": 2,
                 "influence_radius": 8.0, "max_sample_gap": 4.0,
                 "max_time_gap_h": 2.0}

LEAK_ZONE_COLOR = {"own": "#4da3ff", "adjacent": "#f0932b"}


def leak_params(comp_row):
    return {k: comp_row[k] for k in LEAK_PARAM_FIELDS}


def get_leak(conn, lid):
    return row(conn, "SELECT * FROM leak_surveys WHERE id=?", (lid,))


def leak_runs(conn, lid):
    return {r["condition"]: r for r in rows(
        conn, "SELECT * FROM leak_runs WHERE survey_id=?", (lid,))}


def leak_paths(conn, lid):
    out = []
    for p in rows(conn, "SELECT * FROM leak_paths WHERE survey_id=? ORDER BY id", (lid,)):
        out.append({"id": p["id"], "name": p["name"],
                    "vertices": json.loads(p["vertices_json"])})
    return out


def leak_zones(conn, lid):
    out = []
    for z in rows(conn, "SELECT * FROM leak_zones WHERE survey_id=? ORDER BY id", (lid,)):
        out.append({"id": z["id"], "kind": z["kind"], "name": z["name"],
                    "polygon": json.loads(z["polygon_json"])})
    return out


def leak_overrides(conn, lid):
    return rows(conn, "SELECT * FROM leak_pair_overrides WHERE survey_id=? ORDER BY id", (lid,))


def log_leak_event(conn, lid, kind, payload, revision=None):
    sr = row(conn, "SELECT revision FROM leak_surveys WHERE id=?", (lid,))
    rev = revision if revision is not None else (sr["revision"] if sr else 1)
    conn.execute(
        "INSERT INTO leak_events(survey_id,revision,kind,payload_json) VALUES(?,?,?,?)",
        (lid, rev, kind, json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def bump_leak_revision(conn, lid):
    """人工改配/调边界顶点/移点成功后生成并切换到新修订,返回新修订号。"""
    conn.execute("UPDATE leak_surveys SET revision=revision+1 WHERE id=?", (lid,))
    conn.commit()
    return row(conn, "SELECT revision FROM leak_surveys WHERE id=?", (lid,))["revision"]


def recompute_leak(conn, lid):
    """按当前测次/配对表(含人工改配)/限值/边界重算,写 result_json。"""
    surv = get_leak(conn, lid)
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (surv["project_id"],))
    bounds = json.loads(proj["bounds_json"])
    runs = leak_runs(conn, lid)
    on_rows = rows(conn, "SELECT * FROM leak_points WHERE run_id=?",
                   (runs["on"]["id"],)) if "on" in runs else []
    off_rows = rows(conn, "SELECT * FROM leak_points WHERE run_id=?",
                    (runs["off"]["id"],)) if "off" in runs else []
    result = leakage.evaluate_survey(
        on_rows, off_rows, runs, leak_zones(conn, lid), leak_paths(conn, lid),
        leak_params(surv), leak_overrides(conn, lid), bounds)
    conn.execute("UPDATE leak_surveys SET result_json=? WHERE id=?",
                 (json.dumps(result, ensure_ascii=False), lid))
    conn.commit()
    return result


def leak_state(conn, lid):
    surv = get_leak(conn, lid)
    if not surv:
        return None
    proj = row(conn, "SELECT id,name,bounds_json,venue_svg FROM projects WHERE id=?",
               (surv["project_id"],))
    bounds = json.loads(proj["bounds_json"])
    run_rows = rows(conn, "SELECT * FROM leak_runs WHERE survey_id=? ORDER BY id", (lid,))
    point_counts = {}
    for r in run_rows:
        point_counts[r["id"]] = row(
            conn, "SELECT COUNT(*) AS n FROM leak_points WHERE run_id=?", (r["id"],))["n"]
    for r in run_rows:
        r["n_points"] = point_counts[r["id"]]
    events = rows(conn, "SELECT * FROM leak_events WHERE survey_id=? ORDER BY id DESC LIMIT 60",
                  (lid,))
    for e in events:
        e["payload"] = json.loads(e.pop("payload_json"))
    return {
        "id": surv["id"], "project_id": surv["project_id"],
        "project_name": proj["name"], "project_bounds": bounds,
        "venue_svg": proj["venue_svg"],
        "label": surv["label"], "status": surv["status"],
        "revision": surv["revision"],
        "own_loop": surv["own_loop"], "adjacent_loop": surv["adjacent_loop"],
        "params": leak_params(surv),
        "runs": run_rows,
        "zones": leak_zones(conn, lid),
        "paths": leak_paths(conn, lid),
        "overrides": leak_overrides(conn, lid),
        "events": events,
        "created_at": surv["created_at"], "confirmed_at": surv["confirmed_at"],
        "source_on_run_id": surv["source_on_run_id"],
        "source_off_run_id": surv["source_off_run_id"],
        "result": json.loads(surv["result_json"]) if surv["result_json"] else None,
        "nc_text": leakage.NC_TEXT,
    }


@app.route("/api/projects/<int:pid>/leak-surveys", methods=["GET"])
def list_leak_surveys(pid):
    conn = get_db()
    out = []
    for s in rows(conn, "SELECT * FROM leak_surveys WHERE project_id=? ORDER BY id DESC", (pid,)):
        result = json.loads(s["result_json"]) if s["result_json"] else None
        st = result["stats"] if result else None
        out.append({"id": s["id"], "label": s["label"], "status": s["status"],
                    "revision": s["revision"], "own_loop": s["own_loop"],
                    "adjacent_loop": s["adjacent_loop"],
                    "created_at": s["created_at"], "confirmed_at": s["confirmed_at"],
                    "stats": st})
    conn.close()
    return jsonify(out)


@app.route("/api/projects/<int:pid>/leak-surveys", methods=["POST"])
def create_leak_survey(pid):
    data = request.get_json(force=True)
    conn = get_db()
    if not row(conn, "SELECT id FROM projects WHERE id=?", (pid,)):
        conn.close()
        return json_error("项目不存在", 404)
    cur = conn.execute(
        "INSERT INTO leak_surveys(project_id,label,own_loop,adjacent_loop) "
        "VALUES(?,?,?,?)",
        (pid, (data.get("label") or "").strip() or "边界外逸校审",
         (data.get("own_loop") or "").strip() or "本环",
         (data.get("adjacent_loop") or "").strip() or "相邻环"))
    lid = cur.lastrowid
    conn.commit()
    log_leak_event(conn, lid, "create", {"label": data.get("label", "")})
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


@app.route("/api/leak-surveys/<int:lid>")
def get_leak_survey(lid):
    conn = get_db()
    state = leak_state(conn, lid)
    conn.close()
    if not state:
        return json_error("边界外逸校审不存在", 404)
    return jsonify(state)


@app.route("/api/leak-surveys/<int:lid>/params", methods=["PUT"])
def update_leak_params(lid):
    data = request.get_json(force=True)
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,限值已锁定;如需修改请先重审", 409)
    vals = {}
    for k in LEAK_PARAM_FIELDS:
        if k in data and data[k] not in ("", None):
            try:
                vals[k] = float(data[k])
            except (TypeError, ValueError):
                conn.close()
                return json_error("参数 %s 必须为数值" % k)
    if vals.get("min_points", 1) < 1 or vals.get("influence_radius", 1) <= 0 \
            or vals.get("max_sample_gap", 1) <= 0 or vals.get("max_time_gap_h", 1) <= 0:
        conn.close()
        return json_error("插值/半径/间距/时间窗必须为正,最少测点数 >= 1")
    sets = ", ".join(k + "=?" for k in vals)
    conn.execute("UPDATE leak_surveys SET " + sets + " WHERE id=?",
                 (*vals.values(), lid))
    conn.commit()
    if vals:
        recompute_leak(conn, lid)
    log_leak_event(conn, lid, "params", {"changed": vals})
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


# ---------------- 测次导入 / 测点拖动

@app.route("/api/leak-surveys/<int:lid>/runs", methods=["POST"])
def import_leak_run(lid):
    condition = request.form.get("condition")
    if condition not in ("on", "off"):
        return json_error("condition 必须为 on(开启)或 off(关闭)")
    if "csv" not in request.files or not request.files["csv"].filename:
        return json_error("缺少 CSV 文件")
    text = request.files["csv"].read().decode("utf-8-sig", "replace")
    new_rows, errors = csvio.parse_leak_csv(text)
    if not new_rows:
        return json_error("CSV 无有效数据行", 400 if not errors else 422)
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,来源测次已锁定;如需替换请先重审", 409)
    old = row(conn, "SELECT id FROM leak_runs WHERE survey_id=? AND condition=?",
              (lid, condition))
    if old:  # 同工况重导:替换该测次
        conn.execute("DELETE FROM leak_points WHERE run_id=?", (old["id"],))
        conn.execute("DELETE FROM leak_runs WHERE id=?", (old["id"],))
    label = (request.form.get("label") or "").strip() or \
        ("环路开启" if condition == "on" else "环路关闭(背景)")
    time_text = (request.form.get("time_text") or "").strip()
    if not time_text:
        time_text = next((r["time"] for r in new_rows if r.get("time")), "")
    cur = conn.execute(
        "INSERT INTO leak_runs(survey_id,condition,label,time_text,time_iso,device_id) "
        "VALUES(?,?,?,?,?,?)",
        (lid, condition, label, time_text,
         leakage.parse_time(time_text).isoformat() if leakage.parse_time(time_text) else None,
         (new_rows[0].get("device_id") or "")))
    rid = cur.lastrowid
    conn.executemany(
        "INSERT INTO leak_points(run_id,point_label,x,y,field_db,background_db,"
        "calib_version,device_id,moved) VALUES(?,?,?,?,?,?,?,?,0)",
        [(rid, r["point_label"], r["x"], r["y"], r["field_db"], r["background_db"],
          r["calib_version"], r.get("device_id") or "") for r in new_rows])
    conn.commit()
    recompute_leak(conn, lid)
    log_leak_event(conn, lid, "import-run", {
        "condition": condition, "label": label, "time_text": time_text,
        "rows": len(new_rows), "replaced": bool(old), "csv_errors": errors})
    state = leak_state(conn, lid)
    conn.close()
    state["import_result"] = {"condition": condition, "rows": len(new_rows),
                              "replaced": bool(old), "csv_errors": errors}
    return jsonify(state)


@app.route("/api/leak-surveys/<int:lid>/runs/<int:rid>", methods=["DELETE"])
def delete_leak_run(lid, rid):
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,来源测次已锁定;如需删除请先重审", 409)
    r = row(conn, "SELECT * FROM leak_runs WHERE id=? AND survey_id=?", (rid, lid))
    if not r:
        conn.close()
        return json_error("测次不存在", 404)
    conn.execute("DELETE FROM leak_points WHERE run_id=?", (rid,))
    conn.execute("DELETE FROM leak_runs WHERE id=?", (rid,))
    conn.commit()
    recompute_leak(conn, lid)
    log_leak_event(conn, lid, "delete-run", {"condition": r["condition"], "label": r["label"]})
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


@app.route("/api/leak-surveys/<int:lid>/move", methods=["POST"])
def move_leak_point(lid):
    """拖动误定位点。已确认校审拒绝;移动属于新修订,必须备注。"""
    data = request.get_json(force=True)
    label = (data.get("point_label") or "").strip()
    condition = data.get("condition")
    note = (data.get("note") or "").strip()
    if condition not in ("on", "off"):
        return json_error("必须指定 condition=on/off")
    if not note:
        return json_error("调整测点位置必须备注理由,并形成新修订", 422)
    try:
        x, y = float(data["x"]), float(data["y"])
    except (KeyError, TypeError, ValueError):
        return json_error("坐标无法解析")
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,测点位置已锁定;如需调整请先重审", 409)
    rid = row(conn, "SELECT id FROM leak_runs WHERE survey_id=? AND condition=?",
              (lid, condition))["id"]
    pt = row(conn, "SELECT * FROM leak_points WHERE run_id=? AND point_label=?", (rid, label))
    if not pt:
        conn.close()
        return json_error("测点不存在: " + label, 404)
    conn.execute("UPDATE leak_points SET x=?,y=?,moved=1 WHERE id=?", (x, y, pt["id"]))
    conn.commit()
    new_rev = bump_leak_revision(conn, lid)
    recompute_leak(conn, lid)
    log_leak_event(conn, lid, "move", {
        "condition": condition, "point_label": label,
        "from": [pt["x"], pt["y"]], "to": [x, y], "note": note},
        revision=new_rev)
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


# ---------------- 环区与保密边界

@app.route("/api/leak-surveys/<int:lid>/zones", methods=["POST"])
def add_leak_zone(lid):
    data = request.get_json(force=True)
    kind = data.get("kind")
    poly = data.get("polygon")
    if kind not in ("own", "adjacent"):
        return json_error("kind 必须为 own(本环服务区)或 adjacent(相邻环区)")
    if not isinstance(poly, list) or len(poly) < 3:
        return json_error("多边形至少需要 3 个顶点")
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,环区已锁定;如需调整请先重审", 409)
    conn.execute("INSERT INTO leak_zones(survey_id,kind,name,polygon_json) VALUES(?,?,?,?)",
                 (lid, kind, (data.get("name") or "").strip(),
                  json.dumps([[float(a), float(b)] for a, b in poly])))
    conn.commit()
    recompute_leak(conn, lid)
    log_leak_event(conn, lid, "zone-add", {"kind": kind, "name": data.get("name", "")},
                   revision=surv["revision"])
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


@app.route("/api/leak-zones/<int:zid>", methods=["DELETE"])
def delete_leak_zone(zid):
    conn = get_db()
    z = row(conn, "SELECT * FROM leak_zones WHERE id=?", (zid,))
    if not z:
        conn.close()
        return json_error("环区不存在", 404)
    surv = get_leak(conn, z["survey_id"])
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,环区已锁定;如需调整请先重审", 409)
    conn.execute("DELETE FROM leak_zones WHERE id=?", (zid,))
    conn.commit()
    recompute_leak(conn, z["survey_id"])
    log_leak_event(conn, z["survey_id"], "zone-delete",
                   {"kind": z["kind"], "name": z["name"]}, revision=surv["revision"])
    state = leak_state(conn, z["survey_id"])
    conn.close()
    return jsonify(state)


@app.route("/api/leak-surveys/<int:lid>/paths", methods=["POST"])
def add_leak_path(lid):
    data = request.get_json(force=True)
    verts = data.get("vertices")
    if not isinstance(verts, list) or len(verts) < 2:
        return json_error("保密边界至少需要 2 个顶点")
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,边界已锁定;如需调整请先重审", 409)
    cur = conn.execute("INSERT INTO leak_paths(survey_id,name,vertices_json) VALUES(?,?,?)",
                       (lid, (data.get("name") or "").strip(),
                        json.dumps([[float(a), float(b)] for a, b in verts])))
    pid = cur.lastrowid
    conn.commit()
    recompute_leak(conn, lid)
    log_leak_event(conn, lid, "path-add", {"path_id": pid, "name": data.get("name", ""),
                                           "n_vertices": len(verts)},
                   revision=surv["revision"])
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


@app.route("/api/leak-paths/<int:pid>", methods=["DELETE"])
def delete_leak_path(pid):
    conn = get_db()
    p = row(conn, "SELECT * FROM leak_paths WHERE id=?", (pid,))
    if not p:
        conn.close()
        return json_error("边界不存在", 404)
    surv = get_leak(conn, p["survey_id"])
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,边界已锁定;如需调整请先重审", 409)
    conn.execute("DELETE FROM leak_paths WHERE id=?", (pid,))
    conn.commit()
    recompute_leak(conn, p["survey_id"])
    log_leak_event(conn, p["survey_id"], "path-delete", {"path_id": pid, "name": p["name"]},
                   revision=surv["revision"])
    state = leak_state(conn, p["survey_id"])
    conn.close()
    return jsonify(state)


@app.route("/api/leak-paths/<int:pid>/vertices", methods=["PUT"])
def edit_leak_path(pid):
    """编辑边界顶点(拖动/加顶点),或拆分为多段。任何调整必须备注并形成新修订。

    body: {action: "vertices", vertices: [[x,y]...], note}
          {action: "split", at_vertex: k, note}     在第 k 个顶点处拆开
    """
    data = request.get_json(force=True)
    note = (data.get("note") or "").strip()
    if not note:
        return json_error("调整保密边界必须备注理由,并形成新修订", 422)
    conn = get_db()
    p = row(conn, "SELECT * FROM leak_paths WHERE id=?", (pid,))
    if not p:
        conn.close()
        return json_error("边界不存在", 404)
    surv = get_leak(conn, p["survey_id"])
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,边界已锁定;如需调整请先重审", 409)
    action = data.get("action")
    old_verts = json.loads(p["vertices_json"])
    new_ids = []
    if action == "vertices":
        verts = data.get("vertices")
        if not isinstance(verts, list) or len(verts) < 2:
            conn.close()
            return json_error("边界至少需要 2 个顶点")
        verts = [[float(a), float(b)] for a, b in verts]
        conn.execute("UPDATE leak_paths SET vertices_json=? WHERE id=?",
                     (json.dumps(verts), pid))
        payload = {"path_id": pid, "name": p["name"], "note": note,
                   "n_vertices": len(verts)}
    elif action == "split":
        if len(old_verts) < 3:
            conn.close()
            return json_error("拆分至少需要 3 个顶点(内部顶点处拆开,两段各保留端点)")
        try:
            k = int(data.get("at_vertex"))
        except (TypeError, ValueError):
            conn.close()
            return json_error("at_vertex 必须为顶点序号")
        if not (0 < k < len(old_verts) - 1):
            conn.close()
            return json_error("拆分点必须在内部顶点(不含两端)")
        seg_a, seg_b = old_verts[:k + 1], old_verts[k:]
        conn.execute("UPDATE leak_paths SET vertices_json=? WHERE id=?",
                     (json.dumps(seg_a), pid))
        cur = conn.execute("INSERT INTO leak_paths(survey_id,name,vertices_json) VALUES(?,?,?)",
                           (p["survey_id"], (p["name"] or "边界") + "·拆分",
                            json.dumps(seg_b)))
        new_ids.append(cur.lastrowid)
        payload = {"path_id": pid, "name": p["name"], "note": note,
                   "split_at_vertex": k, "new_path_id": new_ids[0]}
    else:
        conn.close()
        return json_error("未知 action(vertices/split)")
    conn.commit()
    new_rev = bump_leak_revision(conn, p["survey_id"])
    recompute_leak(conn, p["survey_id"])
    log_leak_event(conn, p["survey_id"],
                   "path-edit" if action == "vertices" else "path-split", payload,
                   revision=new_rev)
    state = leak_state(conn, p["survey_id"])
    conn.close()
    return jsonify(state)


# ---------------- 人工改配

@app.route("/api/leak-surveys/<int:lid>/overrides", methods=["POST"])
def add_leak_override(lid):
    """人工改配:强制开启测点 ↔ 关闭测点,或取消配对;必须备注,形成新修订。"""
    data = request.get_json(force=True)
    on_label = (data.get("on_label") or "").strip()
    off_label = (data.get("off_label") or "").strip() or None
    note = (data.get("note") or "").strip()
    if not on_label:
        return json_error("缺少开启工况测点号")
    if not note:
        return json_error("人工改配必须备注理由,并形成新修订", 422)
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,配对表已锁定;如需改配请先重审", 409)
    runs = leak_runs(conn, lid)
    if "on" not in runs:
        conn.close()
        return json_error("尚未导入开启工况测次")
    if not row(conn, "SELECT 1 FROM leak_points WHERE run_id=? AND point_label=?",
               (runs["on"]["id"], on_label)):
        conn.close()
        return json_error("开启测次无测点 " + on_label, 404)
    if off_label:
        if "off" not in runs:
            conn.close()
            return json_error("尚未导入关闭工况测次")
        if not row(conn, "SELECT 1 FROM leak_points WHERE run_id=? AND point_label=?",
                   (runs["off"]["id"], off_label)):
            conn.close()
            return json_error("关闭测次无测点 " + off_label, 404)
    conn.execute(
        "INSERT INTO leak_pair_overrides(survey_id,on_label,off_label,note) VALUES(?,?,?,?)",
        (lid, on_label, off_label, note))
    conn.commit()
    new_rev = bump_leak_revision(conn, lid)
    recompute_leak(conn, lid)
    log_leak_event(conn, lid, "override",
                   {"on_label": on_label, "off_label": off_label, "note": note},
                   revision=new_rev)
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


@app.route("/api/leak-surveys/<int:lid>/overrides/<int:oid>", methods=["DELETE"])
def delete_leak_override(lid, oid):
    data = request.get_json(force=True) if request.data else {}
    note = (data.get("note") or "").strip()
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,配对表已锁定;如需改配请先重审", 409)
    if not note:
        conn.close()
        return json_error("撤销人工改配必须备注理由,并形成新修订", 422)
    ov = row(conn, "SELECT * FROM leak_pair_overrides WHERE id=? AND survey_id=?",
             (oid, lid))
    if not ov:
        conn.close()
        return json_error("改配记录不存在", 404)
    conn.execute("DELETE FROM leak_pair_overrides WHERE id=?", (oid,))
    conn.commit()
    new_rev = bump_leak_revision(conn, lid)
    recompute_leak(conn, lid)
    log_leak_event(conn, lid, "override-delete",
                   {"on_label": ov["on_label"], "off_label": ov["off_label"],
                    "note": note},
                   revision=new_rev)
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


# ---------------- 确认锁定 / 重审

@app.route("/api/leak-surveys/<int:lid>/confirm", methods=["POST"])
def confirm_leak(lid):
    """确认:冻结结果,锁定来源测次/配对表/限值/边界;三份导出必须取自此确认结果。"""
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认", 409)
    runs = leak_runs(conn, lid)
    if "on" not in runs or "off" not in runs:
        conn.close()
        return json_error("必须先导入开启与关闭两种工况测次才能确认")
    if not leak_paths(conn, lid):
        conn.close()
        return json_error("至少需要一条保密边界才能确认")
    result = recompute_leak(conn, lid)
    conn.execute(
        "UPDATE leak_surveys SET status='confirmed',confirmed_at=datetime('now'),"
        "source_on_run_id=?,source_off_run_id=? WHERE id=?",
        (runs["on"]["id"], runs["off"]["id"], lid))
    conn.commit()
    log_leak_event(conn, lid, "confirm", {
        "source_on_run_id": runs["on"]["id"],
        "source_off_run_id": runs["off"]["id"],
        "revision": surv["revision"],
        "fail_length_m": result["stats"]["fail_length_m"]},
        revision=surv["revision"])
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


@app.route("/api/leak-surveys/<int:lid>/reopen", methods=["POST"])
def reopen_leak(lid):
    """重审:解除锁定,修订号 +1(此后改配/调边界形成更新修订)。"""
    data = request.get_json(force=True) if request.data else {}
    note = (data.get("note") or "").strip()
    conn = get_db()
    surv = get_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("边界外逸校审不存在", 404)
    if surv["status"] != "confirmed":
        conn.close()
        return json_error("校审尚未确认", 409)
    conn.execute("UPDATE leak_surveys SET status='draft',confirmed_at=NULL,"
                 "revision=revision+1 WHERE id=?", (lid,))
    conn.commit()
    log_leak_event(conn, lid, "reopen", {"note": note or "重审"})
    state = leak_state(conn, lid)
    conn.close()
    return jsonify(state)


# ---------------- 导出(均取自确认结果)

LEAK_STATION_COLOR = {"ok": "#2e9e5b", "fail": "#d24040",
                      "noconclusion": "#9b59b6", "nodata": "#8a8f98",
                      "internal": "#4da3ff"}
LEAK_STATION_TEXT = {"ok": "合格", "fail": "超限", "noconclusion": "无结论",
                     "nodata": "无数据", "internal": "本环区内"}


def confirmed_leak(conn, lid):
    """读取已确认校审的冻结结果;未确认返回 (None, None)。"""
    surv = get_leak(conn, lid)
    if not surv or surv["status"] != "confirmed" or not surv["result_json"]:
        return None, None
    return surv, json.loads(surv["result_json"])


@app.route("/api/leak-surveys/<int:lid>/export/boundary.svg")
def export_leak_svg(lid):
    conn = get_db()
    surv, result = confirmed_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("校审尚未确认,三份导出材料必须取自同一确认结果", 409)
    state = leak_state(conn, lid)
    conn.close()
    bounds = state["project_bounds"]
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.2f %.2f %.2f %.2f" '
             'font-family="sans-serif">' % (bounds["min_x"] - 4, bounds["min_y"] - 6,
                                            bounds["width"] + 8, bounds["height"] + 14)]
    parts.append("<g opacity='0.35'>%s</g>" % svg_inner(state["venue_svg"]))

    for z in state["zones"]:
        pts = " ".join("%.2f,%.2f" % (p[0], p[1]) for p in z["polygon"])
        color = LEAK_ZONE_COLOR[z["kind"]]
        parts.append("<polygon points='%s' fill='%s' fill-opacity='0.05' stroke='%s' "
                     "stroke-width='0.35' stroke-dasharray='1.4 0.9'><title>%s:%s</title>"
                     "</polygon>" % (pts, color, color,
                                     "本环服务区" if z["kind"] == "own" else "相邻环区",
                                     z["name"]))

    # 边界按测站状态逐段着色
    for pr in result["paths"]:
        for run in pr["runs"]:
            parts.append("<circle cx='%.2f' cy='%.2f' r='1.1' fill='#d24040'>"
                         "<title>峰值外逸 %.1f dB @ %.1f m</title></circle>"
                         % (run["peak_x"], run["peak_y"], run["peak_excess"], run["peak_s"]))
        verts = pr["vertices"]
        ss, length = leakage.cumulative_lengths(verts)
        seg = []
        for st in pr["stations"]:
            x, y = leakage.point_at_s(verts, ss, st["s"])
            seg.append((x, y, st["status"]))
        for k in range(len(seg) - 1):
            color = LEAK_STATION_COLOR.get(seg[k][2], "#888")
            dash = "1 0" if seg[k][2] in ("fail", "ok", "internal") else "0.8 0.6"
            parts.append("<line x1='%.2f' y1='%.2f' x2='%.2f' y2='%.2f' stroke='%s' "
                         "stroke-width='0.8' stroke-dasharray='%s'><title>%.1f m %s</title>"
                         "</line>"
                         % (seg[k][0], seg[k][1], seg[k + 1][0], seg[k + 1][1],
                            color, dash, pr["stations"][k]["s"],
                            LEAK_STATION_TEXT.get(seg[k][2], seg[k][2])))
        for i, (vx, vy) in enumerate(verts):
            parts.append("<rect x='%.2f' y='%.2f' width='0.7' height='0.7' fill='#cfd6e2'>"
                         "<title>%s 顶点 %d</title></rect>"
                         % (vx - 0.35, vy - 0.35, pr["name"], i))

    # 配对测点
    for p in result["pairs"]:
        color = "#9b59b6" if p["status"] == "noconclusion" else "#2e9e5b"
        parts.append("<circle cx='%.2f' cy='%.2f' r='0.55' fill='%s' stroke='#fff' "
                     "stroke-width='0.15'><title>%s↔%s %s</title></circle>"
                     % (p["x"], p["y"], color, p["on_label"], p["off_label"],
                        "/".join(leakage.reason_text(r) for r in p["reasons"]) or "配对有效"))
        if p["method"] == "manual":
            parts.append("<circle cx='%.2f' cy='%.2f' r='0.95' fill='none' "
                         "stroke='#ffd75e' stroke-width='0.22'/>" % (p["x"], p["y"]))
    for a in result["ambiguous"]:
        if a["x"] is None:
            continue
        parts.append("<rect x='%.2f' y='%.2f' width='1' height='1' fill='none' "
                     "stroke='#9b59b6' stroke-width='0.22'><title>%s 配对多解,候选:%s"
                     "</title></rect>"
                     % (a["x"] - 0.5, a["y"] - 0.5, a["label"], "/".join(a["candidates"])))

    legend = [("#2e9e5b", "合格"), ("#d24040", "超限外逸"), ("#9b59b6", "无结论"),
              ("#8a8f98", "无数据"), ("#4da3ff", "本环服务区"),
              ("#f0932b", "相邻环区")]
    lx, ly = bounds["min_x"], bounds["min_y"] + bounds["height"] + 2
    for i, (color, txt) in enumerate(legend):
        parts.append("<rect x='%.2f' y='%.2f' width='2' height='2' fill='%s'/>"
                     "<text x='%.2f' y='%.2f' font-size='2.2' fill='#222'>%s</text>"
                     % (lx + i * 13, ly, color, lx + i * 13 + 2.6, ly + 1.8, txt))
    stt = result["stats"]
    peak = stt["peak"] or {}
    parts.append("<text x='%.2f' y='%.2f' font-size='2.4' fill='#222'>%s · %s · 修订 %d · "
                 "超限 %.1f m / 共 %.1f m · 峰值 %.1f dB@%.1f m</text>"
                 % (bounds["min_x"], bounds["min_y"] - 3.2, state["project_name"],
                    state["label"], state["revision"],
                    stt["fail_length_m"], stt["total_length_m"],
                    peak.get("excess", 0) or 0, peak.get("s", 0) or 0))
    parts.append("<text x='%.2f' y='%.2f' font-size='2.0' fill='#222'>外逸限值 %.1f dB · "
                 "边界外绝对场强 ≤ %.1f dB · 确认于 %s</text>"
                 % (bounds["min_x"], bounds["min_y"] - 0.8,
                    result["params"]["leak_limit_db"], result["params"]["max_field_db"],
                    state["confirmed_at"] or ""))
    parts.append("</svg>")
    return Response("".join(parts), mimetype="image/svg+xml", headers={
        "Content-Disposition": "attachment; filename=boundary_l%s_r%d.svg"
                               % (lid, state["revision"])})


@app.route("/api/leak-surveys/<int:lid>/export/remeasure.csv")
def export_leak_csv(lid):
    conn = get_db()
    surv, result = confirmed_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("校审尚未确认,三份导出材料必须取自同一确认结果", 409)
    conn.close()
    head = ["path_id", "path_name", "kind", "s_m", "x", "y", "status",
            "excess_db", "on_field_db", "background_db", "adj_margin_db",
            "n_pairs", "reasons"]
    lines = [",".join(head)]

    def q(s):
        return '"%s"' % str(s).replace('"', '""') if s else ""

    # 复测点:全部超限站 + 无结论/无数据站(前者复测整改、后者补证据)
    for pr in result["paths"]:
        for st in pr["stations"]:
            if st["status"] not in ("fail", "noconclusion", "nodata"):
                continue
            lines.append(",".join([
                str(pr["id"]), q(pr["name"]), st["status"],
                "%.2f" % st["s"], "%.2f" % st["x"], "%.2f" % st["y"],
                st["status"],
                "" if st["excess"] is None else "%.2f" % st["excess"],
                "" if st["on_field"] is None else "%.2f" % st["on_field"],
                "" if st["background"] is None else "%.2f" % st["background"],
                "" if st["adj_margin"] is None else "%.2f" % st["adj_margin"],
                str(st["n_pairs"]),
                q(";".join(leakage.reason_text(r) for r in st["reasons"]))]))
    lines.append("# total_length_m,%.2f" % result["stats"]["total_length_m"])
    lines.append("# fail_length_m,%.2f" % result["stats"]["fail_length_m"])
    lines.append("# noconclusion_length_m,%.2f" % result["stats"]["noconclusion_length_m"])
    return Response("﻿" + "\n".join(lines), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=boundary_remeasure_l%s_r%d.csv"
                               % (lid, surv["revision"])})


@app.route("/api/leak-surveys/<int:lid>/export/recalc.json")
def export_leak_json(lid):
    conn = get_db()
    surv, result = confirmed_leak(conn, lid)
    if not surv:
        conn.close()
        return json_error("校审尚未确认,三份导出材料必须取自同一确认结果", 409)
    state = leak_state(conn, lid)
    conn.close()
    payload = {
        "survey": {k: state[k] for k in
                   ("id", "project_id", "project_name", "label", "status", "revision",
                    "own_loop", "adjacent_loop", "created_at", "confirmed_at",
                    "source_on_run_id", "source_off_run_id")},
        "runs": [{k: r[k] for k in
                  ("id", "condition", "label", "time_text", "time_iso",
                   "device_id", "n_points")} for r in state["runs"]],
        "zones": state["zones"],
        "paths_meta": [{"id": p["id"], "name": p["name"],
                        "n_vertices": len(p["vertices"])} for p in state["paths"]],
        "params": state["params"],
        "overrides": state["overrides"],
        "events": state["events"],
        "result": result,
        "nc_text": leakage.NC_TEXT,
    }
    return Response(json.dumps(payload, ensure_ascii=False, indent=1),
                    mimetype="application/json", headers={
                        "Content-Disposition": "attachment; filename=boundary_recalc_l%s_r%d.json"
                                               % (lid, state["revision"])})


# ---------------------------------------------------------------- 驱动基准

DRIVE_PARAM_FIELDS = ("ref_current_a", "max_sample_gap_s", "max_norm_db",
                      "calib_min_a", "calib_max_a")
DRIVE_DEFAULTS = {"ref_current_a": 2.0, "max_sample_gap_s": 60.0,
                  "max_norm_db": 3.0, "calib_min_a": 0.2, "calib_max_a": 10.0}


def get_drive(conn, did):
    return row(conn, "SELECT * FROM drive_surveys WHERE id=?", (did,))


def drive_params(surv):
    return {k: surv[k] for k in DRIVE_PARAM_FIELDS}


def drive_record(conn, did):
    return row(conn, "SELECT * FROM drive_records WHERE survey_id=? "
                     "ORDER BY id DESC LIMIT 1", (did,))


def drive_samples(conn, record_id):
    return [{"t": s["t_sec"], "t_text": s["t_text"], "current": s["current_a"],
             "clip": s["clip"], "overheat": s["overheat"]}
            for s in rows(conn, "SELECT * FROM drive_samples WHERE record_id=? "
                                 "ORDER BY t_sec", (record_id,))]


def drive_anchors(conn, did):
    return rows(conn, "SELECT * FROM drive_anchors WHERE survey_id=? "
                      "ORDER BY field_t, amp_t", (did,))


def drive_keeps(conn, did):
    return rows(conn, "SELECT * FROM drive_keeps WHERE survey_id=? ORDER BY t0", (did,))


def log_drive_event(conn, did, kind, payload, revision=None):
    sr = row(conn, "SELECT revision FROM drive_surveys WHERE id=?", (did,))
    rev = revision if revision is not None else (sr["revision"] if sr else 1)
    conn.execute(
        "INSERT INTO drive_events(survey_id,revision,kind,payload_json) VALUES(?,?,?,?)",
        (did, rev, kind, json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def bump_drive_revision(conn, did):
    """换绑锚点/保留异常段成功后生成并切换到新修订,返回新修订号。"""
    conn.execute("UPDATE drive_surveys SET revision=revision+1 WHERE id=?", (did,))
    conn.commit()
    return row(conn, "SELECT revision FROM drive_surveys WHERE id=?", (did,))["revision"]


def recompute_drive(conn, did):
    """按当前功放记录/场强记录/锚点/保留段/参数重算,写 result_json。"""
    surv = get_drive(conn, did)
    proj = row(conn, "SELECT * FROM projects WHERE id=?", (surv["project_id"],))
    bounds = json.loads(proj["bounds_json"])
    limits = get_limits(conn, surv["project_id"])
    zones = get_zones(conn, surv["project_id"])
    rec = drive_record(conn, did)
    samples = drive_samples(conn, rec["id"]) if rec else []
    pts = rows(conn, "SELECT * FROM drive_points WHERE survey_id=?", (did,))
    result = drive.evaluate(drive_params(surv), samples, pts,
                            drive_anchors(conn, did), drive_keeps(conn, did),
                            zones, limits, bounds)
    conn.execute("UPDATE drive_surveys SET result_json=? WHERE id=?",
                 (json.dumps(result, ensure_ascii=False), did))
    conn.commit()
    return result


def drive_state(conn, did):
    surv = get_drive(conn, did)
    if not surv:
        return None
    proj = row(conn, "SELECT id,name,bounds_json,venue_svg FROM projects WHERE id=?",
               (surv["project_id"],))
    rec = drive_record(conn, did)
    samples = drive_samples(conn, rec["id"]) if rec else []
    record = None
    if rec:
        record = {"id": rec["id"], "label": rec["label"], "device_id": rec["device_id"],
                  "created_at": rec["created_at"], "n_samples": len(samples),
                  "t0": samples[0]["t"] if samples else None,
                  "t1": samples[-1]["t"] if samples else None,
                  "n_clip": sum(1 for s in samples if s["clip"]),
                  "n_overheat": sum(1 for s in samples if s["overheat"])}
    events = rows(conn, "SELECT * FROM drive_events WHERE survey_id=? "
                        "ORDER BY id DESC LIMIT 60", (did,))
    for e in events:
        e["payload"] = json.loads(e.pop("payload_json"))
    return {
        "id": surv["id"], "project_id": surv["project_id"],
        "project_name": proj["name"], "project_bounds": json.loads(proj["bounds_json"]),
        "venue_svg": proj["venue_svg"],
        "label": surv["label"], "status": surv["status"], "revision": surv["revision"],
        "params": drive_params(surv),
        "record": record, "samples": samples,
        "n_point_rows": row(conn, "SELECT COUNT(*) AS n FROM drive_points "
                                  "WHERE survey_id=?", (did,))["n"],
        "anchors": drive_anchors(conn, did),
        "keeps": drive_keeps(conn, did),
        "events": events,
        "created_at": surv["created_at"], "confirmed_at": surv["confirmed_at"],
        "source_record_id": surv["source_record_id"],
        "result": json.loads(surv["result_json"]) if surv["result_json"] else None,
        "nc_text": drive.NC_TEXT, "keep_kinds": drive.KEEP_KINDS,
    }


@app.route("/api/projects/<int:pid>/drive-surveys", methods=["GET"])
def list_drive_surveys(pid):
    conn = get_db()
    out = []
    for s in rows(conn, "SELECT * FROM drive_surveys WHERE project_id=? "
                        "ORDER BY id DESC", (pid,)):
        result = json.loads(s["result_json"]) if s["result_json"] else None
        st = result["stats"] if result else None
        out.append({"id": s["id"], "label": s["label"], "status": s["status"],
                    "revision": s["revision"], "created_at": s["created_at"],
                    "confirmed_at": s["confirmed_at"], "stats": st})
    conn.close()
    return jsonify(out)


@app.route("/api/projects/<int:pid>/drive-surveys", methods=["POST"])
def create_drive_survey(pid):
    data = request.get_json(force=True)
    conn = get_db()
    if not row(conn, "SELECT id FROM projects WHERE id=?", (pid,)):
        conn.close()
        return json_error("项目不存在", 404)
    cur = conn.execute("INSERT INTO drive_surveys(project_id,label) VALUES(?,?)",
                       (pid, (data.get("label") or "").strip() or "驱动基准校审"))
    did = cur.lastrowid
    conn.commit()
    log_drive_event(conn, did, "create", {"label": data.get("label", "")})
    state = drive_state(conn, did)
    conn.close()
    return jsonify(state)


@app.route("/api/drive-surveys/<int:did>")
def get_drive_survey(did):
    conn = get_db()
    state = drive_state(conn, did)
    conn.close()
    if not state:
        return json_error("驱动基准校审不存在", 404)
    return jsonify(state)


# ---------------- 功放记录 / 场强记录导入

@app.route("/api/drive-surveys/<int:did>/record", methods=["POST"])
def import_drive_record(did):
    """导入功放记录(带时标的环路电流与削波/过热告警);同校审重导即替换。"""
    if "csv" not in request.files or not request.files["csv"].filename:
        return json_error("缺少 CSV 文件")
    text = request.files["csv"].read().decode("utf-8-sig", "replace")
    new_rows, errors = csvio.parse_drive_current_csv(text)
    if not new_rows:
        return json_error("CSV 无有效数据行", 400 if not errors else 422)
    conn = get_db()
    surv = get_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("驱动基准校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,功放记录已锁定;如需替换请先重审", 409)
    old = drive_record(conn, did)
    if old:
        conn.execute("DELETE FROM drive_samples WHERE record_id=?", (old["id"],))
        conn.execute("DELETE FROM drive_records WHERE id=?", (old["id"],))
    label = (request.form.get("label") or "").strip() or "功放记录"
    cur = conn.execute("INSERT INTO drive_records(survey_id,label,device_id) "
                       "VALUES(?,?,?)",
                       (did, label, (request.form.get("device_id") or "").strip()))
    rid = cur.lastrowid
    conn.executemany(
        "INSERT INTO drive_samples(record_id,t_text,t_sec,current_a,clip,overheat) "
        "VALUES(?,?,?,?,?,?)",
        [(rid, r["t_text"], r["t_sec"], r["current_a"], r["clip"], r["overheat"])
         for r in new_rows])
    conn.commit()
    recompute_drive(conn, did)
    log_drive_event(conn, did, "import-record", {
        "label": label, "rows": len(new_rows), "replaced": bool(old),
        "csv_errors": errors})
    state = drive_state(conn, did)
    conn.close()
    state["import_result"] = {"rows": len(new_rows), "replaced": bool(old),
                              "csv_errors": errors}
    return jsonify(state)


@app.route("/api/drive-surveys/<int:did>/record", methods=["DELETE"])
def delete_drive_record(did):
    conn = get_db()
    surv = get_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("驱动基准校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,功放记录已锁定;如需删除请先重审", 409)
    rec = drive_record(conn, did)
    if not rec:
        conn.close()
        return json_error("尚未导入功放记录", 404)
    conn.execute("DELETE FROM drive_samples WHERE record_id=?", (rec["id"],))
    conn.execute("DELETE FROM drive_records WHERE id=?", (rec["id"],))
    conn.commit()
    recompute_drive(conn, did)
    log_drive_event(conn, did, "delete-record", {"label": rec["label"]})
    state = drive_state(conn, did)
    conn.close()
    return jsonify(state)


@app.route("/api/drive-surveys/<int:did>/points", methods=["POST"])
def import_drive_points(did):
    """导入带时标的场强记录;同校审重导即整体替换。"""
    if "csv" not in request.files or not request.files["csv"].filename:
        return json_error("缺少 CSV 文件")
    text = request.files["csv"].read().decode("utf-8-sig", "replace")
    new_rows, errors = csvio.parse_drive_points_csv(text)
    if not new_rows:
        return json_error("CSV 无有效数据行", 400 if not errors else 422)
    conn = get_db()
    surv = get_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("驱动基准校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,场强记录已锁定;如需替换请先重审", 409)
    old_n = row(conn, "SELECT COUNT(*) AS n FROM drive_points WHERE survey_id=?",
                (did,))["n"]
    conn.execute("DELETE FROM drive_points WHERE survey_id=?", (did,))
    conn.executemany(
        "INSERT INTO drive_points(survey_id,point_label,x,y,freq_hz,field_db,noise_db,"
        "t_text,t_sec,device_id,calib_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(did, r["point_label"], r["x"], r["y"], r["freq_hz"], r["field_db"],
          r["noise_db"], r["t_text"], r["t_sec"], r["device_id"], r["calib_version"])
         for r in new_rows])
    conn.commit()
    recompute_drive(conn, did)
    log_drive_event(conn, did, "import-points", {
        "rows": len(new_rows), "replaced": old_n, "csv_errors": errors})
    state = drive_state(conn, did)
    conn.close()
    state["import_result"] = {"rows": len(new_rows), "replaced": old_n,
                              "csv_errors": errors}
    return jsonify(state)


# ---------------- 时钟锚点(换绑须备注,形成新修订)

@app.route("/api/drive-surveys/<int:did>/anchors", methods=["POST"])
def add_drive_anchor(did):
    """绑定/换绑时钟锚点:功放时刻 <-> 场强时刻;必须备注理由,形成新修订。"""
    data = request.get_json(force=True)
    note = (data.get("note") or "").strip()
    if not note:
        return json_error("绑定/换绑锚点必须备注理由,并形成新修订", 422)
    amp_t = drive.parse_clock(data.get("amp_t"))
    field_t = drive.parse_clock(data.get("field_t"))
    if amp_t is None or field_t is None:
        return json_error("锚点时刻无法解析(支持秒数、HH:MM[:SS]、ISO 日期时间)")
    conn = get_db()
    surv = get_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("驱动基准校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,锚点已锁定;如需换绑请先重审", 409)
    cur = conn.execute(
        "INSERT INTO drive_anchors(survey_id,amp_t,field_t,amp_text,field_text,note) "
        "VALUES(?,?,?,?,?,?)",
        (did, amp_t, field_t, str(data.get("amp_t")), str(data.get("field_t")), note))
    aid = cur.lastrowid
    conn.commit()
    new_rev = bump_drive_revision(conn, did)
    recompute_drive(conn, did)
    log_drive_event(conn, did, "anchor", {
        "anchor_id": aid, "amp_t": amp_t, "field_t": field_t, "note": note},
        revision=new_rev)
    state = drive_state(conn, did)
    conn.close()
    return jsonify(state)


@app.route("/api/drive-anchors/<int:aid>", methods=["DELETE"])
def delete_drive_anchor(aid):
    data = request.get_json(force=True) if request.data else {}
    note = (data.get("note") or "").strip()
    if not note:
        return json_error("删除锚点属于换绑,必须备注理由,并形成新修订", 422)
    conn = get_db()
    a = row(conn, "SELECT * FROM drive_anchors WHERE id=?", (aid,))
    if not a:
        conn.close()
        return json_error("锚点不存在", 404)
    surv = get_drive(conn, a["survey_id"])
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,锚点已锁定;如需换绑请先重审", 409)
    conn.execute("DELETE FROM drive_anchors WHERE id=?", (aid,))
    conn.commit()
    new_rev = bump_drive_revision(conn, a["survey_id"])
    recompute_drive(conn, a["survey_id"])
    log_drive_event(conn, a["survey_id"], "anchor-delete", {
        "amp_t": a["amp_t"], "field_t": a["field_t"], "note": note}, revision=new_rev)
    state = drive_state(conn, a["survey_id"])
    conn.close()
    return jsonify(state)


# ---------------- 保留异常段(须备注,形成新修订)

@app.route("/api/drive-surveys/<int:did>/keeps", methods=["POST"])
def add_drive_keep(did):
    """保留异常段:豁免功放时钟 [t0,t1] 内的 clip/overheat/sample-gap 排除。"""
    data = request.get_json(force=True)
    kind = (data.get("kind") or "").strip()
    note = (data.get("note") or "").strip()
    if kind not in drive.KEEP_KINDS:
        return json_error("kind 必须为 " + "/".join(drive.KEEP_KINDS))
    if not note:
        return json_error("保留异常段必须备注理由,并形成新修订", 422)
    t0, t1 = drive.parse_clock(data.get("t0")), drive.parse_clock(data.get("t1"))
    if t0 is None or t1 is None or t1 <= t0:
        return json_error("保留段时刻无法解析或区间为空(t1 必须大于 t0)")
    conn = get_db()
    surv = get_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("驱动基准校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,保留段已锁定;如需调整请先重审", 409)
    cur = conn.execute(
        "INSERT INTO drive_keeps(survey_id,kind,t0,t1,note) VALUES(?,?,?,?,?)",
        (did, kind, t0, t1, note))
    kid = cur.lastrowid
    conn.commit()
    new_rev = bump_drive_revision(conn, did)
    recompute_drive(conn, did)
    log_drive_event(conn, did, "keep", {
        "keep_id": kid, "kind": kind, "t0": t0, "t1": t1, "note": note},
        revision=new_rev)
    state = drive_state(conn, did)
    conn.close()
    return jsonify(state)


@app.route("/api/drive-keeps/<int:kid>", methods=["DELETE"])
def delete_drive_keep(kid):
    data = request.get_json(force=True) if request.data else {}
    note = (data.get("note") or "").strip()
    if not note:
        return json_error("撤销保留段必须备注理由,并形成新修订", 422)
    conn = get_db()
    k = row(conn, "SELECT * FROM drive_keeps WHERE id=?", (kid,))
    if not k:
        conn.close()
        return json_error("保留段不存在", 404)
    surv = get_drive(conn, k["survey_id"])
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,保留段已锁定;如需调整请先重审", 409)
    conn.execute("DELETE FROM drive_keeps WHERE id=?", (kid,))
    conn.commit()
    new_rev = bump_drive_revision(conn, k["survey_id"])
    recompute_drive(conn, k["survey_id"])
    log_drive_event(conn, k["survey_id"], "keep-delete", {
        "kind": k["kind"], "t0": k["t0"], "t1": k["t1"], "note": note},
        revision=new_rev)
    state = drive_state(conn, k["survey_id"])
    conn.close()
    return jsonify(state)


# ---------------- 判定参数

@app.route("/api/drive-surveys/<int:did>/params", methods=["PUT"])
def update_drive_params(did):
    data = request.get_json(force=True)
    conn = get_db()
    surv = get_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("驱动基准校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认,参数已锁定;如需修改请先重审", 409)
    vals = {}
    for k in DRIVE_PARAM_FIELDS:
        if k in data and data[k] not in ("", None):
            try:
                vals[k] = float(data[k])
            except (TypeError, ValueError):
                conn.close()
                return json_error("参数 %s 必须为数值" % k)
    if vals.get("ref_current_a", 1) <= 0 or vals.get("max_sample_gap_s", 1) <= 0 \
            or vals.get("max_norm_db", 1) <= 0:
        conn.close()
        return json_error("参考电流/采样断档/归一化限值必须为正")
    lo = vals.get("calib_min_a", surv["calib_min_a"])
    hi = vals.get("calib_max_a", surv["calib_max_a"])
    if lo <= 0 or hi <= lo:
        conn.close()
        return json_error("校准量程必须为正且上限大于下限")
    if vals:
        sets = ", ".join(k + "=?" for k in vals)
        conn.execute("UPDATE drive_surveys SET " + sets + " WHERE id=?",
                     (*vals.values(), did))
        conn.commit()
        recompute_drive(conn, did)
        log_drive_event(conn, did, "params", {"changed": vals})
    state = drive_state(conn, did)
    conn.close()
    return jsonify(state)


# ---------------- 确认锁定 / 重审

@app.route("/api/drive-surveys/<int:did>/confirm", methods=["POST"])
def confirm_drive(did):
    """确认:冻结结果,锁定功放记录/场强记录/锚点/保留段/参数;导出取自此确认结果。"""
    conn = get_db()
    surv = get_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("驱动基准校审不存在", 404)
    if surv["status"] == "confirmed":
        conn.close()
        return json_error("校审已确认", 409)
    rec = drive_record(conn, did)
    if not rec:
        conn.close()
        return json_error("必须先导入功放记录(环路电流与告警)才能确认")
    if not row(conn, "SELECT 1 FROM drive_points WHERE survey_id=? LIMIT 1", (did,)):
        conn.close()
        return json_error("必须先导入带时标的场强记录才能确认")
    if len(drive_anchors(conn, did)) < 2:
        conn.close()
        return json_error("至少需要 2 个时钟锚点才能生成分段时码映射")
    result = recompute_drive(conn, did)
    conn.execute("UPDATE drive_surveys SET status='confirmed',"
                 "confirmed_at=datetime('now'),source_record_id=? WHERE id=?",
                 (rec["id"], did))
    conn.commit()
    log_drive_event(conn, did, "confirm", {
        "source_record_id": rec["id"], "revision": surv["revision"],
        "n_ok": result["stats"]["n_ok"], "n_excluded": result["stats"]["n_excluded"]},
        revision=surv["revision"])
    state = drive_state(conn, did)
    conn.close()
    return jsonify(state)


@app.route("/api/drive-surveys/<int:did>/reopen", methods=["POST"])
def reopen_drive(did):
    """重审:解除锁定,修订号 +1(此后换绑/保留段形成更新修订)。"""
    data = request.get_json(force=True) if request.data else {}
    note = (data.get("note") or "").strip()
    conn = get_db()
    surv = get_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("驱动基准校审不存在", 404)
    if surv["status"] != "confirmed":
        conn.close()
        return json_error("校审尚未确认", 409)
    conn.execute("UPDATE drive_surveys SET status='draft',confirmed_at=NULL,"
                 "revision=revision+1 WHERE id=?", (did,))
    conn.commit()
    log_drive_event(conn, did, "reopen", {"note": note or "重审"})
    state = drive_state(conn, did)
    conn.close()
    return jsonify(state)


# ---------------- 复测对照(只能引用已确认的驱动版本)

@app.route("/api/projects/<int:pid>/drive-compares", methods=["GET"])
def list_drive_compares(pid):
    conn = get_db()
    survs = {s["id"]: s for s in rows(
        conn, "SELECT id,label,revision,status FROM drive_surveys WHERE project_id=?",
        (pid,))}
    out = []
    for c in rows(conn, "SELECT * FROM drive_compares WHERE project_id=? "
                        "ORDER BY id DESC", (pid,)):
        result = json.loads(c["result_json"]) if c["result_json"] else None
        out.append({
            "id": c["id"], "label": c["label"], "created_at": c["created_at"],
            "base_survey_id": c["base_survey_id"],
            "retest_survey_id": c["retest_survey_id"],
            "base_label": (survs.get(c["base_survey_id"]) or {}).get("label", "?"),
            "retest_label": (survs.get(c["retest_survey_id"]) or {}).get("label", "?"),
            "stats": result["stats"] if result else None,
        })
    conn.close()
    return jsonify(out)


@app.route("/api/projects/<int:pid>/drive-compares", methods=["POST"])
def create_drive_compare(pid):
    """新建驱动对照:基准与复测都必须是已确认的驱动版本(引用其冻结结果)。"""
    data = request.get_json(force=True)
    try:
        base_id, retest_id = int(data["base_survey_id"]), int(data["retest_survey_id"])
    except (KeyError, TypeError, ValueError):
        return json_error("必须指定基准与复测驱动版本")
    if base_id == retest_id:
        return json_error("基准与复测不能是同一驱动版本")
    conn = get_db()
    survs = {s["id"]: s for s in rows(
        conn, "SELECT * FROM drive_surveys WHERE project_id=?", (pid,))}
    base, retest = survs.get(base_id), survs.get(retest_id)
    if base is None or retest is None:
        conn.close()
        return json_error("所选驱动版本不属于本项目", 404)
    if base["status"] != "confirmed" or retest["status"] != "confirmed":
        conn.close()
        return json_error("复测对照只能引用已确认的驱动版本;请先确认两份校审", 409)
    result = drive.compare_results(json.loads(base["result_json"]),
                                   json.loads(retest["result_json"]))
    cur = conn.execute(
        "INSERT INTO drive_compares(project_id,base_survey_id,retest_survey_id,label,"
        "result_json) VALUES(?,?,?,?,?)",
        (pid, base_id, retest_id,
         (data.get("label") or "").strip() or "驱动对照 #%s→#%s" % (base_id, retest_id),
         json.dumps(result, ensure_ascii=False)))
    cid = cur.lastrowid
    conn.commit()
    for sid in (base_id, retest_id):
        log_drive_event(conn, sid, "compare-ref",
                        {"compare_id": cid, "base_survey_id": base_id,
                         "retest_survey_id": retest_id})
    state = drive_compare_state(conn, cid)
    conn.close()
    return jsonify(state)


def drive_compare_state(conn, cid):
    c = row(conn, "SELECT * FROM drive_compares WHERE id=?", (cid,))
    if not c:
        return None
    survs = {s["id"]: s for s in rows(
        conn, "SELECT id,label,revision,status,confirmed_at FROM drive_surveys "
              "WHERE project_id=?", (c["project_id"],))}
    return {
        "id": c["id"], "project_id": c["project_id"], "label": c["label"],
        "created_at": c["created_at"],
        "base_survey_id": c["base_survey_id"],
        "retest_survey_id": c["retest_survey_id"],
        "base_survey": survs.get(c["base_survey_id"]),
        "retest_survey": survs.get(c["retest_survey_id"]),
        "result": json.loads(c["result_json"]) if c["result_json"] else None,
    }


@app.route("/api/drive-compares/<int:cid>")
def get_drive_compare(cid):
    conn = get_db()
    state = drive_compare_state(conn, cid)
    conn.close()
    if not state:
        return json_error("驱动对照不存在", 404)
    return jsonify(state)


# ---------------- 导出(均取自确认结果,可追到电流样本与换算值)

DRIVE_CELL_COLOR = {"ok": "#2e9e5b", "warn": "#d8a012", "fail": "#d24040",
                    "nodata": "#8a8f98", "noconclusion": "#9b59b6",
                    "notest": "#3a3f47"}


def confirmed_drive(conn, did):
    """读取已确认校审的冻结结果;未确认返回 (None, None)。"""
    surv = get_drive(conn, did)
    if not surv or surv["status"] != "confirmed" or not surv["result_json"]:
        return None, None
    return surv, json.loads(surv["result_json"])


@app.route("/api/drive-surveys/<int:did>/export/drive.svg")
def export_drive_svg(did):
    conn = get_db()
    surv, result = confirmed_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("校审尚未确认,三份导出材料必须取自同一确认结果", 409)
    state = drive_state(conn, did)
    conn.close()
    bounds = state["project_bounds"]
    params = state["params"]

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.2f %.2f %.2f %.2f" '
             'font-family="sans-serif">' % (bounds["min_x"] - 4, bounds["min_y"] - 6,
                                            bounds["width"] + 8, bounds["height"] + 12)]
    parts.append("<g opacity='0.35'>%s</g>" % svg_inner(state["venue_svg"]))
    cs = result["grid"]["cs"]
    for c in result["cells"]:
        parts.append(
            "<rect x='%.2f' y='%.2f' width='%.2f' height='%.2f' fill='%s' "
            "fill-opacity='0.55'><title>%s %s</title></rect>"
            % (c["x"] - cs / 2, c["y"] - cs / 2, cs, cs,
               DRIVE_CELL_COLOR.get(c["status"], "#888"), c["status"], c["reason"]))
    for p in result["points"]:
        color = "#1c6dd9" if p["status"] == "ok" else "#9b59b6"
        parts.append("<circle cx='%.2f' cy='%.2f' r='0.55' fill='%s' stroke='#fff' "
                     "stroke-width='0.15'><title>%s %s 原始 %.1f→归一 %s dB</title></circle>"
                     % (p["x"], p["y"], color, p["label"],
                        "合格" if p["status"] == "ok" else
                        "/".join(drive.reason_text(r) for r in p["reasons"]),
                        p["raw_ref_db"],
                        "%.1f" % p["norm_ref_db"] if p["norm_ref_db"] is not None else "—"))
        if p["kept"]:
            parts.append("<circle cx='%.2f' cy='%.2f' r='0.95' fill='none' "
                         "stroke='#ffd75e' stroke-width='0.25'/>" % (p["x"], p["y"]))
    legend = [("#1c6dd9", "归一化合格测点"), ("#9b59b6", "被排除测点"),
              ("#ffd75e", "保留异常段"), ("#2e9e5b", "覆盖合格"),
              ("#d24040", "覆盖不合格"), ("#8a8f98", "证据不足")]
    lx, ly = bounds["min_x"], bounds["min_y"] + bounds["height"] + 2
    for i, (color, txt) in enumerate(legend):
        parts.append("<rect x='%.2f' y='%.2f' width='2' height='2' fill='%s'/>"
                     "<text x='%.2f' y='%.2f' font-size='2.2' fill='#222'>%s</text>"
                     % (lx + i * 16, ly, color, lx + i * 16 + 2.6, ly + 1.8, txt))
    stt = result["stats"]
    parts.append("<text x='%.2f' y='%.2f' font-size='2.4' fill='#222'>%s · %s · 修订 %d · "
                 "测点 %d(排除 %d)· 参考电流 %.2f A</text>"
                 % (bounds["min_x"], bounds["min_y"] - 3.2, state["project_name"],
                    state["label"], state["revision"], stt["n_points"],
                    stt["n_excluded"], params["ref_current_a"]))
    parts.append("<text x='%.2f' y='%.2f' font-size='2.0' fill='#222'>场强已按 20·log10("
                 "I参考/I实际) 归一化 · 确认于 %s</text>"
                 % (bounds["min_x"], bounds["min_y"] - 0.8, state["confirmed_at"] or ""))
    parts.append("</svg>")
    return Response("".join(parts), mimetype="image/svg+xml", headers={
        "Content-Disposition": "attachment; filename=drive_d%s_r%d.svg"
                               % (did, state["revision"])})


@app.route("/api/drive-surveys/<int:did>/export/points.csv")
def export_drive_csv(did):
    conn = get_db()
    surv, result = confirmed_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("校审尚未确认,三份导出材料必须取自同一确认结果", 409)
    conn.close()
    head = ["point_id", "freq_hz", "field_time", "field_t_s", "amp_t_s",
            "current_a", "ref_current_a", "correction_db",
            "sample_lo_t_s", "sample_lo_a", "sample_hi_t_s", "sample_hi_a",
            "raw_field_db", "norm_field_db", "status", "reasons", "kept"]
    lines = [",".join(head)]
    ref_i = result["params"]["ref_current_a"]

    def q(s):
        return '"%s"' % str(s).replace('"', '""') if s else ""

    def num(v, nd=3):
        return "" if v is None else ("%.*f" % (nd, v))

    for r in result["rows"]:
        lo, hi = r["sample_lo"] or {}, r["sample_hi"] or {}
        lines.append(",".join([
            r["label"], "%g" % r["freq_hz"], q(r["t_text"]), num(r["field_t"], 1),
            num(r["amp_t"], 1), num(r["current_a"]), "%.3f" % ref_i,
            num(r["correction_db"], 2),
            num(lo.get("t"), 1), num(lo.get("current")),
            num(hi.get("t"), 1), num(hi.get("current")),
            num(r["raw_db"], 2), num(r["norm_db"], 2),
            "excluded" if r["reasons"] else "ok",
            q(";".join(drive.reason_text(c) for c in r["reasons"])),
            q(";".join(drive.reason_text(c) for c in r["kept"]))]))
    lines.append("# ref_current_a,%.3f" % ref_i)
    lines.append("# 归一化: norm_field_db = raw_field_db + 20*log10(ref_current_a/current_a)")
    return Response("﻿" + "\n".join(lines), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=drive_points_d%s_r%d.csv"
                               % (did, surv["revision"])})


@app.route("/api/drive-surveys/<int:did>/export/recalc.json")
def export_drive_json(did):
    conn = get_db()
    surv, result = confirmed_drive(conn, did)
    if not surv:
        conn.close()
        return json_error("校审尚未确认,三份导出材料必须取自同一确认结果", 409)
    state = drive_state(conn, did)
    conn.close()
    payload = {
        "survey": {k: state[k] for k in
                   ("id", "project_id", "project_name", "label", "status", "revision",
                    "created_at", "confirmed_at", "source_record_id")},
        "record": state["record"],
        "params": state["params"],
        "anchors": state["anchors"],
        "keeps": state["keeps"],
        "samples": state["samples"],
        "events": state["events"],
        "result": result,
        "nc_text": drive.NC_TEXT,
        "normalization": "norm_field_db = raw_field_db + 20*log10(ref_current_a/current_a)",
    }
    return Response(json.dumps(payload, ensure_ascii=False, indent=1),
                    mimetype="application/json", headers={
                        "Content-Disposition": "attachment; filename=drive_recalc_d%s_r%d.json"
                                               % (did, state["revision"])})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
