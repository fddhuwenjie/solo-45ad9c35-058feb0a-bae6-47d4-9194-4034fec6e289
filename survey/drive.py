"""驱动基准:功放环路电流记录 + 时钟锚点 -> 分段时码映射 -> 测点归一化。

现场问题:一次剧场巡测常要走上几十分钟,功放可能限幅、过热或输入电平改变
导致输出漂移;若把各时刻读数当作同一驱动条件,弱场区可能只是当时环路电流下降。

流程(对应现场做法):
  1. 上传带时标的环路电流与削波/过热告警(功放记录);
  2. 上传带时标的场强记录(巡测读数);
  3. 为功放时钟与场强时钟设置锚点,生成分段时码映射;
  4. 测点按映射归入电流区间,取插值电流,以冻结的参考电流归一化场强
     (磁场与环路电流成正比:校正值 dB = 20*log10(I_ref / I_actual)),
     原始读数始终保留展示;
  5. 映射倒退 / 采样断档 / 削波 / 过热 / 校准量程不足 / 归一化幅度越限的
     测点不进入覆盖计算;保留异常段(clip/overheat/sample-gap)须备注理由。
"""
import bisect
import math
import re

from survey.leakage import parse_time
from survey.spatial import aggregate_points, compute_grid

EPS = 1e-9

# 测点排除原因代码 -> 中文说明(供前端与导出使用)
NC_TEXT = {
    "no-mapping": "超出锚点映射范围",
    "map-regression": "映射倒退",
    "sample-gap": "采样断档",
    "clip": "削波告警",
    "overheat": "过热",
    "calib-range": "校准量程不足",
    "norm-range": "归一化幅度越限",
}

# 可通过「保留异常段」豁免的原因(时间区段型异常)
KEEP_KINDS = ("clip", "overheat", "sample-gap")

_CLOCK_RE = re.compile(r"^(\d{1,3}):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?$")


def parse_clock(text):
    """宽松解析时刻为秒:纯数字秒 / HH:MM[:SS[.f]] / ISO 日期时间。失败返回 None。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    m = _CLOCK_RE.match(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        ss = float(m.group(3) or 0.0)
        if mm < 60 and ss < 60:
            return hh * 3600 + mm * 60 + ss
        return None
    dt = parse_time(s)
    return dt.timestamp() if dt else None


def reason_text(code):
    """'clip' -> '削波告警'(供导出可读列)。"""
    k, _, arg = code.partition(":")
    t = NC_TEXT.get(k, k)
    return t + ("(" + arg + ")" if arg else "")


# ---------------------------------------------------------------- 分段时码映射

def build_mapping(anchors):
    """按时钟锚点生成分段时码映射(场强时刻 -> 功放时刻)。

    anchors: [{amp_t, field_t, ...}](秒)。返回按场强时刻排序的线段列表
    [{f0,f1,a0,a1,backwards}];任一段两侧时刻不同时严格递增即「映射倒退」。
    """
    anch = sorted((dict(a) for a in anchors), key=lambda a: (a["field_t"], a["amp_t"]))
    segs = []
    for i in range(len(anch) - 1):
        a0, a1 = anch[i], anch[i + 1]
        backwards = not (a1["field_t"] > a0["field_t"] + EPS
                         and a1["amp_t"] > a0["amp_t"] + EPS)
        segs.append({"f0": a0["field_t"], "f1": a1["field_t"],
                     "a0": a0["amp_t"], "a1": a1["amp_t"], "backwards": backwards})
    return segs


def map_field_time(segments, t):
    """场强时刻 t -> (功放时刻, backwards)。无映射(锚点不足/超出范围)返回 (None, False)。"""
    for s in segments:
        lo, hi = min(s["f0"], s["f1"]), max(s["f0"], s["f1"])
        if lo - EPS <= t <= hi + EPS:
            if s["backwards"]:
                return None, True
            span = s["f1"] - s["f0"]
            r = 0.0 if span <= EPS else (t - s["f0"]) / span
            return s["a0"] + r * (s["a1"] - s["a0"]), False
    return None, False


# ---------------------------------------------------------------- 电流采样

def sample_current(samples, t, max_gap):
    """在电流样本(已按 t 升序)中求 t 处的插值电流。

    返回 (current, lo, hi, gap):gap=True 表示采样断档(含超出记录范围、
    相邻样本间距超过 max_gap);lo/hi 为夹逼样本(可追溯换算依据)。
    """
    if not samples:
        return None, None, None, True
    if t < samples[0]["t"] - EPS or t > samples[-1]["t"] + EPS:
        return None, None, None, True
    ts = [s["t"] for s in samples]
    i = bisect.bisect_left(ts, t)
    if i < len(ts) and abs(ts[i] - t) <= EPS:
        s = samples[i]
        return s["current"], s, s, False
    lo, hi = samples[i - 1], samples[i]
    if hi["t"] - lo["t"] > max_gap + EPS:
        return None, lo, hi, True
    span = hi["t"] - lo["t"]
    cur = lo["current"] if span <= EPS else \
        lo["current"] + (hi["current"] - lo["current"]) * (t - lo["t"]) / span
    return cur, lo, hi, False


# ---------------------------------------------------------------- 单时刻求值

def eval_moment(field_t, segments, samples, params, keeps):
    """单个场强时刻的映射与电流求值。

    返回 {amp_t, current, correction, reasons, kept, lo, hi}:
    reasons 为命中且未被保留段豁免的排除原因代码;kept 为被保留段豁免的原因。
    采样断档分两种:两侧有样本可插值的,保留段豁免后按跨档插值换算
    (测点有 current_a/correction,可进入覆盖计算);超出记录范围或无样本的,
    无可换算电流,保留段也不能豁免,保持禁入。
    """
    out = {"amp_t": None, "current": None, "correction": None,
           "reasons": [], "kept": [], "lo": None, "hi": None}
    amp_t, backwards = map_field_time(segments, field_t)
    if backwards:
        out["reasons"].append("map-regression")
        return out
    if amp_t is None:
        out["reasons"].append("no-mapping")
        return out
    out["amp_t"] = amp_t
    cur, lo, hi, gap = sample_current(samples, amp_t, params["max_sample_gap_s"])
    out["lo"], out["hi"] = lo, hi
    if gap:
        if lo is None or hi is None:
            # 超出记录范围或无样本:无可换算电流,保留段也不能豁免
            out["reasons"].append("sample-gap")
            return out
        # 断档但两侧有样本:仅保留段豁免时才接受跨档插值
        if not any(k["kind"] == "sample-gap" and k["t0"] - EPS <= amp_t <= k["t1"] + EPS
                   for k in keeps):
            out["reasons"].append("sample-gap")
            return out
        out["kept"].append("sample-gap")
        span = hi["t"] - lo["t"]
        cur = lo["current"] if span <= EPS else \
            lo["current"] + (hi["current"] - lo["current"]) * (amp_t - lo["t"]) / span
    out["current"] = cur
    if (lo and lo["clip"]) or (hi and hi["clip"]):
        out["reasons"].append("clip")
    if (lo and lo["overheat"]) or (hi and hi["overheat"]):
        out["reasons"].append("overheat")
    if cur is None or cur <= 0 \
            or cur < params["calib_min_a"] - EPS or cur > params["calib_max_a"] + EPS:
        out["reasons"].append("calib-range")
    if cur and cur > 0:
        corr = 20.0 * math.log10(params["ref_current_a"] / cur)
        out["correction"] = corr
        if abs(corr) > params["max_norm_db"] + EPS:
            out["reasons"].append("norm-range")
    # 保留异常段:命中保留段的对应原因豁免(仍记入 kept 供审计)
    for k in keeps:
        if k["kind"] in out["reasons"] \
                and k["t0"] - EPS <= amp_t <= k["t1"] + EPS:
            out["reasons"].remove(k["kind"])
            out["kept"].append(k["kind"])
    return out


# ---------------------------------------------------------------- 主入口

def evaluate(params, samples, point_rows, anchors, keeps, zones, limits, bounds):
    """驱动基准校审主入口。

    params: {ref_current_a, max_sample_gap_s, max_norm_db, calib_min_a, calib_max_a}
    samples: [{t, current, clip, overheat, t_text}] 功放电流样本
    point_rows: drive_points DB 行(带时标的场强记录)
    anchors/keeps: 时钟锚点 / 保留异常段
    返回完整 result dict(存 result_json,前端与三份导出共用)。
    """
    samples = sorted((dict(s) for s in samples), key=lambda s: s["t"])
    segments = build_mapping(anchors)
    gaps = []
    for i in range(1, len(samples)):
        d = samples[i]["t"] - samples[i - 1]["t"]
        if d > params["max_sample_gap_s"] + EPS:
            gaps.append({"t0": samples[i - 1]["t"], "t1": samples[i]["t"],
                         "gap_s": round(d, 3)})

    groups = {}
    for r in point_rows:
        rr = dict(r)
        rr["eval"] = eval_moment(float(r["t_sec"]), segments, samples, params, keeps)
        groups.setdefault(r["point_label"], []).append(rr)

    points, rows_out = [], []
    for label in sorted(groups):
        rs = groups[label]
        reasons = sorted({c for rr in rs for c in rr["eval"]["reasons"]})
        kept = sorted({c for rr in rs for c in rr["eval"]["kept"]})
        ref_rr = min(rs, key=lambda rr: abs(float(rr["freq_hz"]) - limits["ref_freq"]))
        e = ref_rr["eval"]
        norm_ref = round(float(ref_rr["field_db"]) + e["correction"], 3) \
            if e["correction"] is not None else None
        points.append({
            "label": label,
            "x": float(rs[0]["x"]), "y": float(rs[0]["y"]),
            "t_text": ref_rr["t_text"], "field_t": float(ref_rr["t_sec"]),
            "amp_t": e["amp_t"], "current_a": e["current"],
            "ref_current_a": params["ref_current_a"],
            "correction_db": e["correction"],
            "raw_ref_db": float(ref_rr["field_db"]), "norm_ref_db": norm_ref,
            "ref_freq": float(ref_rr["freq_hz"]),
            "device_id": rs[0]["device_id"] or "",
            "calib_version": rs[0]["calib_version"] or "",
            "n_rows": len(rs),
            "status": "excluded" if reasons else "ok",
            "reasons": reasons, "kept": kept,
            "sample_lo": e["lo"], "sample_hi": e["hi"],
            "freqs": {"%g" % float(rr["freq_hz"]): {
                "raw": float(rr["field_db"]),
                "correction": rr["eval"]["correction"],
                "norm": (round(float(rr["field_db"]) + rr["eval"]["correction"], 3)
                         if rr["eval"]["correction"] is not None else None),
            } for rr in rs},
        })
        for rr in rs:
            ev = rr["eval"]
            rows_out.append({
                "label": label, "freq_hz": float(rr["freq_hz"]),
                "t_text": rr["t_text"], "field_t": float(rr["t_sec"]),
                "amp_t": ev["amp_t"], "current_a": ev["current"],
                "correction_db": ev["correction"],
                "raw_db": float(rr["field_db"]),
                "norm_db": (round(float(rr["field_db"]) + ev["correction"], 3)
                            if ev["correction"] is not None else None),
                "reasons": list(ev["reasons"]), "kept": list(ev["kept"]),
                "sample_lo": ev["lo"], "sample_hi": ev["hi"],
            })

    # 合格测点以归一化场强进入覆盖计算;被排除测点一律不参与
    meas = []
    for label, rs in groups.items():
        if any(rr["eval"]["reasons"] for rr in rs):
            continue
        for rr in rs:
            corr = rr["eval"]["correction"]
            if corr is None:  # 无电流换算值不得进入覆盖计算(双保险)
                continue
            meas.append({
                "point_label": label, "x": float(rr["x"]), "y": float(rr["y"]),
                "freq_hz": float(rr["freq_hz"]),
                "field_db": round(float(rr["field_db"]) + corr, 3),
                "noise_db": float(rr["noise_db"]),
                "device_id": rr["device_id"] or "",
                "calib_version": rr["calib_version"] or "",
                "locked": 0, "excluded": 0, "exclude_reason": "", "moved": 0,
            })
    agg = aggregate_points(meas, limits["expected_freqs"], limits["ref_freq"], bounds) \
        if meas else []
    cells, grid_meta = compute_grid(agg, zones, limits, bounds)

    reason_counts = {}
    for p in points:
        for c in p["reasons"]:
            reason_counts[c] = reason_counts.get(c, 0) + 1
    cell_stats = {}
    for c in cells:
        cell_stats[c["status"]] = cell_stats.get(c["status"], 0) + 1
    return {
        "params": dict(params),
        "anchors": [dict(a) for a in anchors],
        "mapping": {"segments": segments,
                    "ok": bool(segments) and not any(s["backwards"] for s in segments)},
        "keeps": [dict(k) for k in keeps],
        "samples_summary": {
            "n": len(samples),
            "t0": samples[0]["t"] if samples else None,
            "t1": samples[-1]["t"] if samples else None,
            "n_clip": sum(1 for s in samples if s["clip"]),
            "n_overheat": sum(1 for s in samples if s["overheat"]),
            "gaps": gaps[:50],
        },
        "points": points,
        "rows": rows_out,
        "cells": cells,
        "grid": grid_meta,
        "stats": {
            "n_points": len(points),
            "n_ok": sum(1 for p in points if p["status"] == "ok"),
            "n_excluded": sum(1 for p in points if p["status"] == "excluded"),
            "n_kept": sum(1 for p in points if p["kept"]),
            "reasons": reason_counts,
            "n_anchors": len(anchors),
            "map_ok": bool(segments) and not any(s["backwards"] for s in segments),
            "cells": cell_stats,
        },
    }


# ---------------------------------------------------------------- 复测对照

def compare_results(base_res, retest_res):
    """两份已确认驱动结果的逐点对照(按点号配对,比归一化参考频点场强)。"""
    bp = {p["label"]: p for p in base_res["points"]}
    rp = {p["label"]: p for p in retest_res["points"]}
    pairs, skipped = [], []
    for lb in sorted(set(bp) & set(rp)):
        b, r = bp[lb], rp[lb]
        if b["status"] != "ok" or r["status"] != "ok" \
                or b["norm_ref_db"] is None or r["norm_ref_db"] is None:
            skipped.append({"label": lb, "base_status": b["status"],
                            "retest_status": r["status"]})
            continue
        pairs.append({
            "label": lb, "x": b["x"], "y": b["y"],
            "base_norm_db": b["norm_ref_db"], "retest_norm_db": r["norm_ref_db"],
            "delta_db": round(r["norm_ref_db"] - b["norm_ref_db"], 3),
            "base_raw_db": b["raw_ref_db"], "retest_raw_db": r["raw_ref_db"],
            "base_current_a": b["current_a"], "retest_current_a": r["current_a"],
        })
    deltas = [abs(p["delta_db"]) for p in pairs]
    return {
        "pairs": pairs, "skipped": skipped,
        "base_only": sorted(set(bp) - set(rp)),
        "retest_only": sorted(set(rp) - set(bp)),
        "stats": {
            "n_pairs": len(pairs), "n_skipped": len(skipped),
            "n_base_only": len(set(bp) - set(rp)),
            "n_retest_only": len(set(rp) - set(bp)),
            "max_abs_delta_db": round(max(deltas), 3) if deltas else None,
            "mean_abs_delta_db": round(sum(deltas) / len(deltas), 3) if deltas else None,
        },
    }
