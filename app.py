"""助听感应环巡测校审 — Flask 后端。

职责:解析测量 CSV、空间计算(调用 survey.spatial)、SQLite 版本存取、
限值校核、增量重算、补测路径规划与三类导出(覆盖 SVG / 补测清单 / 复算 JSON)。
复测对照:跨轮次配对与差异计算(调用 survey.compare),确认后冻结结果并导出。
"""
import json
import re

from flask import Flask, Response, jsonify, render_template, request

from survey import compare, csvio, spatial
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
