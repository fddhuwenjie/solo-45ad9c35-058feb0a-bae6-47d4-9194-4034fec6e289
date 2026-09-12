"""助听感应环巡测校审 — Flask 后端。

职责:解析测量 CSV、空间计算(调用 survey.spatial)、SQLite 版本存取、
限值校核、增量重算、补测路径规划与三类导出(覆盖 SVG / 补测清单 / 复算 JSON)。
"""
import json
import re

from flask import Flask, Response, jsonify, render_template, request

from survey import csvio, spatial
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
    conn.close()
    payload = {
        "project": {"id": state["project"]["id"], "name": state["project"]["name"],
                    "bounds": state["project"]["bounds"]},
        "version": {"id": ver["id"], "label": ver["label"],
                    "created_at": ver["created_at"]},
        "limits": state["limits"],
        "calibration_summary": state["version"]["calibration_summary"],
        "manual_decisions": state["decisions"],
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
