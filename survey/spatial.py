"""空间计算:测点聚合校验、IDW 插值网格、限值校核、补测区域聚类与最短路径。"""
import math
from collections import defaultdict

EPS = 1e-9

# 测点状态问题代码 -> 中文说明(供前端与导出使用)
ISSUE_TEXT = {
    "out-of-bounds": "坐标越界",
    "duplicate-conflict": "同名测点坐标冲突",
    "duplicate-freq": "同一测点频点重复",
    "duplicate-point": "与他点距离过近(疑似重复)",
    "missing-freq": "频点缺失",
}


def point_in_poly(x, y, poly):
    """射线法判断点是否在多边形内,poly 为 [[x, y], ...]。"""
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / ((yj - yi) or EPS) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def _close_freq(freqs_map, target, tol=0.06):
    """在已测频点中找与 target 相对误差不超过 tol 的键,找不到返回 None。"""
    best, bd = None, None
    for f in freqs_map:
        d = abs(f - target) / max(abs(target), EPS)
        if bd is None or d < bd:
            best, bd = f, d
    return best if bd is not None and bd <= tol else None


def point_metrics(p, expected_freqs, ref_freq):
    """单点指标:参考频点场强、信噪比、频响最大偏差。数据不全返回 None。"""
    ref_key = _close_freq(p["freqs"], ref_freq)
    if ref_key is None:
        return None
    f_ref = p["freqs"][ref_key]["field"]
    n_ref = p["freqs"][ref_key]["noise"]
    dev = 0.0
    for ef in expected_freqs:
        k = _close_freq(p["freqs"], ef)
        if k is not None:
            dev = max(dev, abs(p["freqs"][k]["field"] - f_ref))
    return {"field": round(f_ref, 3), "snr": round(f_ref - n_ref, 3), "freq_dev": round(dev, 3)}


def aggregate_points(measurements, expected_freqs, ref_freq, bounds, dup_eps=0.5):
    """把测量行按 point_label 聚合为测点,并执行数据有效性校验。

    校验规则(命中即 valid=False,该点不参与任何网格结论):
      - 坐标越出场地边界
      - 同一标签出现不同坐标(重复测点冲突)
      - 同一标签同一频点重复
      - 缺少任一期望频点
      - 与另一标签空间距离 < dup_eps(疑似重复测点)
    """
    groups = defaultdict(list)
    for m in measurements:
        groups[m["point_label"]].append(m)

    points = []
    for label in sorted(groups):
        rows = groups[label]
        p = {
            "label": label,
            "x": float(rows[0]["x"]),
            "y": float(rows[0]["y"]),
            "device_id": rows[0]["device_id"] or "",
            "calib_version": rows[0]["calib_version"] or "",
            "locked": bool(max(r["locked"] for r in rows)),
            "excluded": bool(max(r["excluded"] for r in rows)),
            "exclude_reason": rows[0]["exclude_reason"] or "",
            "moved": bool(max(r["moved"] for r in rows)),
            "freqs": {},
            "valid": True,
            "issues": [],
        }
        if len({(round(r["x"], 3), round(r["y"], 3)) for r in rows}) > 1:
            p["valid"] = False
            p["issues"].append("duplicate-conflict")
        if not (bounds["min_x"] - EPS <= p["x"] <= bounds["min_x"] + bounds["width"] + EPS
                and bounds["min_y"] - EPS <= p["y"] <= bounds["min_y"] + bounds["height"] + EPS):
            p["valid"] = False
            p["issues"].append("out-of-bounds")
        for r in rows:
            f = float(r["freq_hz"])
            if any(abs(f - k) < 1e-6 for k in p["freqs"]):
                p["valid"] = False
                p["issues"].append("duplicate-freq")
                continue
            p["freqs"][f] = {"field": float(r["field_db"]), "noise": float(r["noise_db"])}
        missing = [ef for ef in expected_freqs if _close_freq(p["freqs"], ef) is None]
        if missing:
            p["valid"] = False
            p["issues"].append("missing-freq:" + ",".join(str(int(m)) for m in missing))
        points.append(p)

    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            a, b = points[i], points[j]
            if math.hypot(a["x"] - b["x"], a["y"] - b["y"]) < dup_eps:
                for p in (a, b):
                    p["valid"] = False
                    if "duplicate-point" not in p["issues"]:
                        p["issues"].append("duplicate-point")

    for p in points:
        p["issues"] = sorted(set(p["issues"]))
        p["metrics"] = point_metrics(p, expected_freqs, ref_freq) if p["valid"] else None
    return points


def compute_grid(points, zones, limits, bounds, prev_cells=None, affected=None):
    """计算覆盖网格。

    prev_cells + affected:增量重算——仅重算 affected 中的格子,其余沿用 prev_cells。
    返回 (cells, grid_meta)。status 取值:
      ok / warn(临近干扰) / fail(限值不合格) / nodata(证据不足) /
      noconclusion(校准版本混用,不得下结论) / notest(禁测区)
    """
    cs = float(limits["cell_size"])
    R = float(limits["influence_radius"])
    min_pts = int(limits["min_points"])
    nx = max(1, math.ceil(bounds["width"] / cs))
    ny = max(1, math.ceil(bounds["height"] / cs))
    aud = [z["polygon"] for z in zones if z["kind"] == "audience"]
    notest = [z["polygon"] for z in zones if z["kind"] == "notest"]
    inter = [z["polygon"] for z in zones if z["kind"] == "interference"]
    usable = [p for p in points if p["valid"] and not p["excluded"] and p["metrics"]]
    prev = {(c["cx"], c["cy"]): c for c in (prev_cells or [])}

    cells = []
    for j in range(ny):
        for i in range(nx):
            if affected is not None and (i, j) not in affected:
                if (i, j) in prev:
                    cells.append(prev[(i, j)])
                continue
            x = bounds["min_x"] + (i + 0.5) * cs
            y = bounds["min_y"] + (j + 0.5) * cs
            cell = {"cx": i, "cy": j, "x": round(x, 3), "y": round(y, 3),
                    "field": None, "snr": None, "uniformity": None, "freq_dev": None,
                    "status": "outside", "reason": "", "n": 0}
            if any(point_in_poly(x, y, p) for p in notest):
                cell["status"] = "notest"
                cell["reason"] = "禁测区"
            elif aud and not any(point_in_poly(x, y, p) for p in aud):
                cell["status"] = "outside"
            else:
                contrib = [(p, math.hypot(p["x"] - x, p["y"] - y)) for p in usable]
                contrib = [(p, d) for p, d in contrib if d <= R]
                cell["n"] = len(contrib)
                if len(contrib) < min_pts:
                    cell["status"] = "nodata"
                    cell["reason"] = "证据不足:有效测点 %d/%d" % (len(contrib), min_pts)
                else:
                    calibs = {p["calib_version"] for p, _ in contrib}
                    if len(calibs) > 1:
                        cell["status"] = "noconclusion"
                        cell["reason"] = "校准版本混用:" + "/".join(sorted(calibs))
                    else:
                        ws = [1.0 / max(d, 0.25) ** 2 for _, d in contrib]
                        wsum = sum(ws)
                        field = sum(w * p["metrics"]["field"] for (p, _), w in zip(contrib, ws)) / wsum
                        snr = sum(w * p["metrics"]["snr"] for (p, _), w in zip(contrib, ws)) / wsum
                        fdev = sum(w * p["metrics"]["freq_dev"] for (p, _), w in zip(contrib, ws)) / wsum
                        fields = [p["metrics"]["field"] for p, _ in contrib]
                        unif = max(fields) - min(fields)
                        cell.update(field=round(field, 2), snr=round(snr, 2),
                                    uniformity=round(unif, 2), freq_dev=round(fdev, 2))
                        problems = []
                        if not (limits["field_min"] <= field <= limits["field_max"]):
                            problems.append("场强越限 %.1f dB" % field)
                        if snr < limits["snr_min"]:
                            problems.append("信噪比不足 %.1f dB" % snr)
                        if unif > limits["uniformity_db"]:
                            problems.append("均匀度超标 %.1f dB" % unif)
                        if fdev > limits["freq_dev_db"]:
                            problems.append("频响偏差超标 %.1f dB" % fdev)
                        if problems:
                            cell["status"] = "fail"
                            cell["reason"] = ";".join(problems)
                        elif any(point_in_poly(x, y, p) for p in inter):
                            cell["status"] = "warn"
                            cell["reason"] = "临近干扰设备,结果待人工确认"
                        else:
                            cell["status"] = "ok"
            if cell["status"] != "outside":
                cells.append(cell)
    return cells, {"nx": nx, "ny": ny, "cs": cs}


def affected_cells(locations, limits, bounds):
    """由变更点位置集合计算需要重算的格子集合(含影响半径外扩半格)。"""
    cs = float(limits["cell_size"])
    reach = float(limits["influence_radius"]) + cs * math.sqrt(2) / 2
    nx = max(1, math.ceil(bounds["width"] / cs))
    ny = max(1, math.ceil(bounds["height"] / cs))
    aff = set()
    for (x, y) in locations:
        i0 = max(0, int((x - reach - bounds["min_x"]) // cs))
        i1 = min(nx - 1, int((x + reach - bounds["min_x"]) // cs))
        j0 = max(0, int((y - reach - bounds["min_y"]) // cs))
        j1 = min(ny - 1, int((y + reach - bounds["min_y"]) // cs))
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                aff.add((i, j))
    return aff


def remeasure_stops(cells):
    """把 nodata/fail/noconclusion 格子做四连通聚类,每类质心为一个补测站。"""
    bad = {(c["cx"], c["cy"]): c for c in cells
           if c["status"] in ("nodata", "fail", "noconclusion")}
    seen = set()
    stops = []
    for key in bad:
        if key in seen:
            continue
        stack = [key]
        seen.add(key)
        members = []
        while stack:
            k = stack.pop()
            members.append(bad[k])
            i, j = k
            for nb in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if nb in bad and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        xs = [m["x"] for m in members]
        ys = [m["y"] for m in members]
        stops.append({
            "x": round(sum(xs) / len(xs), 2),
            "y": round(sum(ys) / len(ys), 2),
            "cells": len(members),
            "statuses": sorted({m["status"] for m in members}),
            "reasons": sorted({m["reason"] for m in members if m["reason"]})[:4],
        })
    return stops


def tsp_order(stops):
    """最近邻 + 2-opt 求近似最短补测路径,返回 (有序站点, 总里程)。"""
    if not stops:
        return [], 0.0
    remaining = list(stops)
    cur = min(remaining, key=lambda s: s["x"] ** 2 + s["y"] ** 2)
    order = [cur]
    remaining.remove(cur)
    while remaining:
        nxt = min(remaining, key=lambda s: (s["x"] - cur["x"]) ** 2 + (s["y"] - cur["y"]) ** 2)
        order.append(nxt)
        remaining.remove(nxt)
        cur = nxt

    def d(a, b):
        return math.hypot(a["x"] - b["x"], a["y"] - b["y"])

    def total(path):
        return sum(d(path[k], path[k + 1]) for k in range(len(path) - 1))

    improved = True
    while improved:
        improved = False
        for i in range(len(order) - 1):
            for k in range(i + 1, len(order)):
                a = d(order[i - 1], order[i]) if i > 0 else 0.0
                b = d(order[k], order[k + 1]) if k + 1 < len(order) else 0.0
                c = d(order[i - 1], order[k]) if i > 0 else 0.0
                e = d(order[i], order[k + 1]) if k + 1 < len(order) else 0.0
                if a + b - c - e > 1e-9:
                    order[i:k + 1] = reversed(order[i:k + 1])
                    improved = True
    for idx, s in enumerate(order, 1):
        s["seq"] = idx
    return order, round(total(order), 2)
