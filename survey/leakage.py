"""边界外逸:相邻排练厅感应环同时启用时,本环磁场越过保密边界串入相邻接收器的校审。

流程(对应现场做法):
  1. 导入环路「开启 on」与「关闭 off」两种工况的带坐标场强读数(两种工况错时测量,时段不重叠);
  2. 测点先按点号、再按位置互唯一配对(复用复测对照的配对逻辑),多重匹配保持无结论;
  3. 用关闭工况读数沿边界里程 IDW 插值估计背景,开启工况插值场强,差值即外逸量 dB;
  4. 沿保密边界逐站给出:连续超限长度、峰值位置、相邻环余量。

以下任一情形,相关边界段(测站)保持「无结论」,不输出合格/超限判定:
  - 开/关测次时间窗不重叠(time-gap,两种工况时段不重叠)
  - 测点配对多解(ambiguous)
  - 边界路径自交(self-intersect)
  - 沿线采样间距过大(sample-gap)
  - 配对此起彼伏的校准依据不兼容(calib-mismatch)
"""
import math
from collections import defaultdict
from datetime import datetime

from survey.compare import apply_overrides, auto_pair
from survey.spatial import point_in_poly

EPS = 1e-9

# 无结论原因代码 -> 中文说明(供前端与导出使用)
NC_TEXT = {
    "time-gap": "两种工况时段不重叠",
    "ambiguous": "测点配对多解",
    "self-intersect": "边界路径自交",
    "sample-gap": "采样间距过大",
    "calib-mismatch": "校准依据不兼容",
    "invalid-point": "配对测点数据无效",
    "no-pair": "影响半径内无配对测点",
}

STATION_DS = 1.0  # 沿线测站间距 m

TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%Y-%m-%d", "%Y/%m/%d",
)


def parse_time(text):
    """宽松解析测量时间字符串;无法解析(含只有时刻没有日期)返回 None。"""
    if not text:
        return None
    s = text.strip().replace("Z", "")
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def reason_text(code):
    """'ambiguous:P03' -> '测点配对多解(P03)'。"""
    k, _, arg = code.partition(":")
    t = NC_TEXT.get(k, k)
    return t + ("(" + arg + ")" if arg else "")


# ---------------------------------------------------------------- 测点载入

def load_run_points(rows, bounds):
    """把 leak_points 行按 point_label 聚合(每点一条场强读数)。

    返回 {label: point};同标签坐标冲突、越界或重复行标 valid=False。
    """
    groups = defaultdict(list)
    for r in rows:
        groups[r["point_label"]].append(r)
    pts = {}
    for label in sorted(groups):
        rs = groups[label]
        p = {
            "label": label,
            "x": float(rs[0]["x"]), "y": float(rs[0]["y"]),
            "field": float(rs[0]["field_db"]),
            "background": rs[0]["background_db"],
            "calib_version": rs[0]["calib_version"] or "",
            "device_id": rs[0]["device_id"] or "",
            "moved": bool(max(r["moved"] for r in rs)),
            "valid": True, "issues": [],
        }
        if p["background"] is not None:
            p["background"] = float(p["background"])
        if len({(round(r["x"], 3), round(r["y"], 3)) for r in rs}) > 1:
            p["valid"] = False
            p["issues"].append("duplicate-conflict")
        if not (bounds["min_x"] - EPS <= p["x"] <= bounds["min_x"] + bounds["width"] + EPS
                and bounds["min_y"] - EPS <= p["y"] <= bounds["min_y"] + bounds["height"] + EPS):
            p["valid"] = False
            p["issues"].append("out-of-bounds")
        if len(rs) > 1:
            p["issues"].append("duplicate-label")
            p["valid"] = False
        pts[label] = p
    return pts


# ---------------------------------------------------------------- 折线几何

def cumulative_lengths(verts):
    """返回各顶点累计里程 [s0=0, s1, ...] 与总长。"""
    ss = [0.0]
    for i in range(1, len(verts)):
        ss.append(ss[-1] + math.hypot(verts[i][0] - verts[i - 1][0],
                                      verts[i][1] - verts[i - 1][1]))
    return ss, ss[-1]


def point_at_s(verts, ss, s):
    """里程 s 处的折线坐标。"""
    if s <= 0:
        return verts[0][0], verts[0][1]
    for i in range(1, len(verts)):
        if s <= ss[i] + EPS:
            seg = max(ss[i] - ss[i - 1], EPS)
            t = (s - ss[i - 1]) / seg
            return (verts[i - 1][0] + t * (verts[i][0] - verts[i - 1][0]),
                    verts[i - 1][1] + t * (verts[i][1] - verts[i - 1][1]))
    return verts[-1][0], verts[-1][1]


def project_to_path(x, y, verts, ss):
    """点到折线的投影,返回 (里程 s, 垂直距离 d, 落段索引 seg)。

    测点可能在空间上靠近折线的另一支路(U 形两臂),但沿路径里程很远;
    调用方须同时用垂距与里程差(展开距离)隔离这类样本。
    """
    best = None
    for i in range(1, len(verts)):
        ax, ay = verts[i - 1]
        bx, by = verts[i]
        vx, vy = bx - ax, by - ay
        seg2 = vx * vx + vy * vy
        t = ((x - ax) * vx + (y - ay) * vy) / seg2 if seg2 > EPS else 0.0
        t = min(1.0, max(0.0, t))
        px, py = ax + t * vx, ay + t * vy
        d = math.hypot(x - px, y - py)
        s = ss[i - 1] + t * math.sqrt(seg2)
        if best is None or d < best[1]:
            best = (s, d, i - 1)
    return best


def _segments_cross(p1, p2, p3, p4):
    """线段 p1p2 与 p3p4 是否规范相交(不含共享端点)。"""
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = cross(p3, p4, p1), cross(p3, p4, p2)
    d3, d4 = cross(p1, p2, p3), cross(p1, p2, p4)
    if ((d1 > EPS and d2 < -EPS) or (d1 < -EPS and d2 > EPS)) and \
       ((d3 > EPS and d4 < -EPS) or (d3 < -EPS and d4 > EPS)):
        return True

    # 端点落在另一线段内部(T 接,折线自交)。用点积判断在线段包围盒内,
    # 对水平/竖直退化线段同样成立(配合 cross≈0)。
    def on(a, b, c):
        if abs(cross(a, b, c)) >= EPS:
            return False
        t = ((c[0] - a[0]) * (b[0] - a[0]) + (c[1] - a[1]) * (b[1] - a[1]))
        l2 = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
        return EPS < t < l2 - EPS
    if on(p3, p4, p1) or on(p3, p4, p2) or on(p1, p2, p3) or on(p1, p2, p4):
        return True
    return False


def path_self_intersects(verts):
    """非相邻段相交(含 T 接/回到旧顶点)即自交;首尾相接的闭合路径不算。"""
    n = len(verts)
    closed = n >= 4 and math.hypot(verts[0][0] - verts[-1][0],
                                   verts[0][1] - verts[-1][1]) < EPS

    def same(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1]) < EPS

    for i in range(n - 1):
        for j in range(i + 1, n - 1):
            if j == i + 1:  # 相邻段共享顶点,允许
                continue
            if closed and i == 0 and j == n - 2:
                continue  # 闭合环的首尾段
            if _segments_cross(verts[i], verts[i + 1], verts[j], verts[j + 1]):
                return True
            # 非相邻段端点重合(回折/分支)
            for a in (verts[i], verts[i + 1]):
                for b in (verts[j], verts[j + 1]):
                    if same(a, b):
                        return True
    return False


# ---------------------------------------------------------------- 配对

def pair_runs(on_pts, off_pts, pos_tol, overrides):
    """开/关工况测点配对。返回 (pairs, ambiguous, on_only, off_only)。

    自动配对:点号一致 → 位置互唯一(容差 pos_tol);再叠加人工改配。
    每条 pair 附 status:ok / noconclusion(+reasons 列表,可多重)。
    测次级的时段不重叠在 evaluate_survey 统一判定后追加。
    """
    auto, ambiguous, on_only, off_only = auto_pair(on_pts, off_pts, pos_tol)
    ov = [{"base_label": o["on_label"], "retest_label": o.get("off_label"),
           "note": o["note"]} for o in (overrides or [])]
    auto, ambiguous, on_only, off_only = apply_overrides(
        auto, ambiguous, on_only, off_only, ov,
        {p["label"] for p in on_pts}, {p["label"] for p in off_pts})

    pairs = []
    for pr in auto:
        op, fp = on_pts_map(pr, on_pts), off_pts_map(pr, off_pts)
        if op is None or fp is None:
            continue
        reasons = []
        if not op["valid"] or not fp["valid"]:
            reasons.append("invalid-point")
        bcal = op["calib_version"] or "未标注"
        rcal = fp["calib_version"] or "未标注"
        if bcal != rcal:
            reasons.append("calib-mismatch")
        pairs.append({
            "on_label": op["label"], "off_label": fp["label"],
            "x": op["x"], "y": op["y"], "rx": fp["x"], "ry": fp["y"],
            "on_field": op["field"], "off_field": fp["field"],
            "method": pr["method"], "dist": pr.get("dist"),
            "manual_note": pr.get("note"),
            "on_calib": op["calib_version"], "off_calib": fp["calib_version"],
            "on_device": op["device_id"], "off_device": fp["device_id"],
            "status": "noconclusion" if reasons else "ok",
            "reasons": reasons,
        })
    pairs.sort(key=lambda p: (p["status"] != "ok", p["on_label"]))
    return pairs, ambiguous, sorted(on_only), sorted(off_only)


def on_pts_map(pr, pts):
    return next((p for p in pts if p["label"] == pr["base_label"]), None)


def off_pts_map(pr, pts):
    return next((p for p in pts if p["label"] == pr["retest_label"]), None)


# ---------------------------------------------------------------- 沿线插值

def _idw(items):
    """items: [(value, weight_dist)],反距离加权。"""
    ws = [1.0 / max(d, 0.25) ** 2 for _, d in items]
    return sum(w * v for (v, _), w in zip(items, ws)) / sum(ws)


def _local_span(s, projections):
    """本站处的采样覆盖跨度(m):本站两侧最近配对测点的投影里程间距。

    - 两侧都有:右侧最近 − 左侧最近(测点恰在本站时分别向更外侧再找);
    - 只有一侧:本站到最近测点距离的 2 倍;
    - 测点恰在本站且无其他测点:0(本站本身有采样,间距不超限,
      但点数不足由 min_points 另行判定);
    - 无测点:None。
    """
    left = sorted((t for t in projections if t < s - EPS), reverse=True)
    right = sorted(t for t in projections if t > s + EPS)
    on_station = any(abs(t - s) <= EPS for t in projections)
    if on_station:
        hl = s - left[0] if left else None
        hr = right[0] - s if right else None
        if hl is not None and hr is not None:
            return hl + hr
        if hl is not None:
            return 2 * hl
        if hr is not None:
            return 2 * hr
        return 0.0
    if not left and not right:
        return None
    if not left:
        return 2 * right[0] - 2 * s
    if not right:
        return 2 * (s - left[0])
    return right[0] - left[0]


def _station_bin(idx, n, length):
    """第 idx 个测站代表的里程区间长度(端点站延伸到 0/L)。"""
    s = min(length, idx * STATION_DS)
    lo = 0.0 if idx == 0 else s - STATION_DS / 2
    hi = length if idx == n else s + STATION_DS / 2
    return max(0.0, hi - lo)


def evaluate_path(path, pairs, ambiguous, on_pts, off_pts, zones, params):
    """沿一条保密边界逐站计算外逸量。返回该路径结果 dict。"""
    verts = path["vertices"]
    ss, length = cumulative_lengths(verts)
    self_cross = len(verts) >= 4 and path_self_intersects(verts)
    own_polys = [z["polygon"] for z in zones if z["kind"] == "own"]
    adj_polys = [z["polygon"] for z in zones if z["kind"] == "adjacent"]
    R = params["influence_radius"]

    ok_pairs = [p for p in pairs if p["status"] == "ok"]
    bad_pairs = [p for p in pairs if p["status"] == "noconclusion"]
    proj_ok = [(p, project_to_path(p["x"], p["y"], verts, ss)) for p in ok_pairs]
    proj_bad = [(p, project_to_path(p["x"], p["y"], verts, ss)) for p in bad_pairs]
    # 多重匹配可发生在开/关任一侧;取该点实际坐标与影响半径比较
    amb_proj = []
    for a in ambiguous:
        src = on_pts if a["side"] == "base" else off_pts
        q = src.get(a["label"])
        if q is not None:
            amb_proj.append((a, q, project_to_path(q["x"], q["y"], verts, ss)))

    n = max(1, int(round(length / STATION_DS)))
    stations = []
    for k in range(n + 1):
        s = min(length, k * STATION_DS)
        x, y = point_at_s(verts, ss, s)
        st = {"s": round(s, 2), "x": round(x, 2), "y": round(y, 2),
              "on_field": None, "background": None, "excess": None,
              "on_margin": None, "adj_margin": None,
              "n_pairs": 0, "sample_span": None,
              "in_own": any(point_in_poly(x, y, p) for p in own_polys),
              "in_adjacent": any(point_in_poly(x, y, p) for p in adj_polys),
              "status": "ok", "reasons": []}

        # 取点范围:垂距与沿路径里程差都必须 <= 影响半径。
        # 仅按欧氏距离会把 U 形另一支路(空间近、沿线远)的样本混入本站。
        def along(pr, s):
            ps, pd = pr[0], pr[1]
            return abs(pd) <= R + EPS and abs(ps - s) <= R + EPS

        near_ok = [(p, pr[0], pr[1]) for (p, pr) in proj_ok if along(pr, s)]
        near_bad = [p for (p, pr) in proj_bad if along(pr, s)]
        near_amb = [(a, q) for (a, q, pr) in amb_proj if along(pr, s)]

        # 无结论原因叠加(可多重)
        for p in near_bad:
            for code in p["reasons"]:
                if code not in st["reasons"]:
                    st["reasons"].append(code)
        for a, _ in near_amb:
            st["reasons"].append("ambiguous:" + a["label"])
        if self_cross:
            st["reasons"].append("self-intersect")
        if near_ok:
            span = _local_span(s, sorted(ps for _, ps, _ in near_ok))
            st["sample_span"] = round(span, 2) if span is not None else None
            if span is not None and span > params["max_sample_gap"] + EPS:
                st["reasons"].append("sample-gap")

        # 有有效配对时仍插值给出参考值(无结论站在曲线上画虚点)。
        # IDW 权重用展开距离 sqrt(垂距² + 里程差²),而非直线欧氏距离。
        if near_ok:
            weights = [(p, max(math.sqrt(pd ** 2 + (s - ps) ** 2), EPS))
                       for p, ps, pd in near_ok]
            on_v = _idw([(p["on_field"], d) for p, d in weights])
            bg_v = _idw([(p["off_field"], d) for p, d in weights])
            st["on_field"] = round(on_v, 2)
            st["background"] = round(bg_v, 2)
            st["excess"] = round(on_v - bg_v, 2)
            st["on_margin"] = round(params["max_field_db"] - on_v, 2)
            st["n_pairs"] = len(near_ok)

        if st["in_own"]:
            st["status"] = "internal"           # 本环服务区内部不评外逸
        elif self_cross:
            st["status"] = "noconclusion"        # 路径自交:里程投影不唯一,全线无结论
        elif not near_ok and not near_bad and not near_amb:
            st["status"] = "nodata"
            st["reasons"].append("no-pair")
        elif len(near_ok) < params["min_points"]:
            st["status"] = "noconclusion"
            if "sample-gap" not in st["reasons"]:
                st["reasons"].append("sample-gap")
        elif st["reasons"]:
            st["status"] = "noconclusion"
        else:
            over = st["excess"] > params["leak_limit_db"] + EPS
            over_abs = st["on_field"] > params["max_field_db"] + EPS
            if over:
                st["reasons"].append("外逸量 %.1f > 限值 %.1f dB"
                                     % (st["excess"], params["leak_limit_db"]))
            if over_abs:
                st["reasons"].append("边界外场强 %.1f > 限值 %.1f dB"
                                     % (st["on_field"], params["max_field_db"]))
            st["status"] = "fail" if (over or over_abs) else "ok"
        if st["in_adjacent"] and st["excess"] is not None:
            st["adj_margin"] = round(min(params["leak_limit_db"] - st["excess"],
                                         params["max_field_db"] - st["on_field"]), 2)
        stations.append(st)

    # 连续超限段(fail 站的极大连续游程,里程按测站代表区间累计)
    runs, i = [], 0
    while i < len(stations):
        if stations[i]["status"] != "fail":
            i += 1
            continue
        j = i
        while j + 1 < len(stations) and stations[j + 1]["status"] == "fail":
            j += 1
        members = stations[i:j + 1]
        peak = max(members, key=lambda t: t["excess"])
        run_len = sum(_station_bin(k, n, length) for k in range(i, j + 1))
        adj = [t["adj_margin"] for t in members if t["adj_margin"] is not None]
        runs.append({
            "s0": members[0]["s"], "s1": members[-1]["s"],
            "length_m": round(run_len, 2),
            "peak_s": peak["s"], "peak_x": peak["x"], "peak_y": peak["y"],
            "peak_excess": peak["excess"],
            "min_adj_margin": min(adj) if adj else None,
        })
        i = j + 1

    def len_of(pred):
        return round(sum(_station_bin(k, n, length)
                         for k, t in enumerate(stations) if pred(t)), 2)

    concl = [t for t in stations if t["excess"] is not None
             and t["status"] in ("ok", "fail")]
    peak = max(concl, key=lambda t: t["excess"]) if concl else None
    adjm = [t["adj_margin"] for t in concl if t["adj_margin"] is not None]
    return {
        "id": path["id"], "name": path["name"], "vertices": verts,
        "length_m": round(length, 2), "self_intersect": self_cross,
        "stations": stations, "runs": runs,
        "peak": None if peak is None else {
            "s": peak["s"], "x": peak["x"], "y": peak["y"],
            "excess": peak["excess"], "status": peak["status"]},
        "min_adj_margin": min(adjm) if adjm else None,
        "stats": {
            "ok_m": len_of(lambda t: t["status"] == "ok"),
            "fail_m": len_of(lambda t: t["status"] == "fail"),
            "noconclusion_m": len_of(lambda t: t["status"] == "noconclusion"),
            "nodata_m": len_of(lambda t: t["status"] == "nodata"),
            "internal_m": len_of(lambda t: t["status"] == "internal"),
            "n_ok": sum(t["status"] == "ok" for t in stations),
            "n_fail": sum(t["status"] == "fail" for t in stations),
            "n_nc": sum(t["status"] == "noconclusion" for t in stations),
            "n_nodata": sum(t["status"] == "nodata" for t in stations),
            "n_internal": sum(t["status"] == "internal" for t in stations),
            "n_runs": len(runs),
        },
    }


# ---------------------------------------------------------------- 主入口

def evaluate_survey(on_rows, off_rows, runs, zones, paths, params, overrides, bounds):
    """边界外逸校审主入口。

    on_rows/off_rows: 该测次的 leak_points DB 行
    runs: {condition: run_row}(含 time_text)
    paths: [{id, name, vertices}]
    返回完整 result dict(存 result_json,前端与三份导出共用)。
    """
    on_pts = load_run_points(on_rows, bounds)
    off_pts = load_run_points(off_rows, bounds)
    pos_tol = min(params["influence_radius"], params["max_sample_gap"])

    # 测次级时间窗:两测次时间都可解析且相隔过久 -> 两种工况时段不重叠
    run_time_gap = None
    t_on = parse_time((runs.get("on") or {}).get("time_text"))
    t_off = parse_time((runs.get("off") or {}).get("time_text"))
    if t_on and t_off and abs((t_on - t_off).total_seconds()) > 3600 * params["max_time_gap_h"]:
        run_time_gap = abs((t_on - t_off).total_seconds()) / 3600.0

    pairs, ambiguous, on_only, off_only = pair_runs(
        list(on_pts.values()), list(off_pts.values()), pos_tol, overrides)
    if run_time_gap is not None:
        for p in pairs:
            if "time-gap" not in p["reasons"]:
                p["reasons"].append("time-gap")
            p["status"] = "noconclusion"

    # 多重匹配点补坐标(开/关任一侧),供平面图与导出标注
    amb_out = []
    for a in ambiguous:
        src = on_pts if a["side"] == "base" else off_pts
        q = src.get(a["label"])
        amb_out.append({
            "label": a["label"],
            "side": "on" if a["side"] == "base" else "off",
            "candidates": a["candidates"],
            "x": q["x"] if q else None, "y": q["y"] if q else None,
        })

    path_results = [evaluate_path(pa, pairs, ambiguous, on_pts, off_pts, zones, params)
                    for pa in paths]

    total_len = sum(pr["length_m"] for pr in path_results)
    fail_len = sum(pr["stats"]["fail_m"] for pr in path_results)
    nc_len = sum(pr["stats"]["noconclusion_m"] for pr in path_results)
    peaks = [pr["peak"] for pr in path_results if pr["peak"]]
    global_peak = None
    if peaks:
        gp = max(peaks, key=lambda t: t["excess"])
        gp_path = next(pr for pr in path_results if pr["peak"] is gp)
        global_peak = dict(gp, path_id=gp_path["id"], path_name=gp_path["name"])
    adjm = [pr["min_adj_margin"] for pr in path_results if pr["min_adj_margin"] is not None]
    return {
        "params": {k: params[k] for k in
                   ("leak_limit_db", "max_field_db", "min_points", "influence_radius",
                    "max_sample_gap", "max_time_gap_h")},
        "run_time_gap_h": round(run_time_gap, 2) if run_time_gap is not None else None,
        "pairs": pairs,
        "ambiguous": amb_out,
        "unpaired": {"on_only": on_only, "off_only": off_only},
        "unpaired_points": [
            {"label": lb, "condition": "on", "x": on_pts[lb]["x"], "y": on_pts[lb]["y"]}
            for lb in on_only] + [
            {"label": lb, "condition": "off", "x": off_pts[lb]["x"], "y": off_pts[lb]["y"]}
            for lb in off_only],
        "paths": path_results,
        "stats": {
            "n_on_points": len(on_pts), "n_off_points": len(off_pts),
            "n_pairs": len(pairs),
            "n_concluded": sum(p["status"] == "ok" for p in pairs),
            "n_nc_pairs": sum(p["status"] == "noconclusion" for p in pairs),
            "n_ambiguous": len(ambiguous),
            "n_on_only": len(on_only), "n_off_only": len(off_only),
            "n_paths": len(path_results), "total_length_m": round(total_len, 2),
            "fail_length_m": round(fail_len, 2),
            "noconclusion_length_m": round(nc_len, 2),
            "peak": global_peak,
            "min_adj_margin": min(adjm) if adjm else None,
        },
    }
