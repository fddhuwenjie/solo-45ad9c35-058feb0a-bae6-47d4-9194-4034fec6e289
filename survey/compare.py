"""复测对照:两轮巡测的测点配对、逐频率差值/余量/判定迁移计算与成片退化聚类。

配对顺序:先按点号(标签)一一对应,再对剩余测点按位置容差互唯一匹配;
多重匹配(任一侧候选 != 1)保持无结论。配对成功后依次校验:
  - 测点数据有效性(任一轮无效即无结论)
  - 校准依据兼容(校准版本不一致即无结论)
  - 频率集合一致(任一轮频点无法一一对应即无结论)
三项校验全过才逐频率计算:读数差、距阈值余量的增减、判定迁移。
"""
import math

from survey.spatial import _close_freq, issue_label

FREQ_TOL = 0.06  # 与 spatial._close_freq 一致的频点匹配容差(相对误差)

# 判定迁移 -> 中文说明(供前端与导出使用)
MIGRATION_TEXT = {
    "ok->ok": "保持合格",
    "ok->fail": "退化(合格→不合格)",
    "fail->ok": "改善(不合格→合格)",
    "fail->fail": "保持不合格",
    "none": "无结论",
}

# 无结论原因代码 -> 中文说明
NC_TEXT = {
    "ambiguous": "多重匹配",
    "invalid-point": "测点数据无效",
    "calib-mismatch": "校准依据不兼容",
    "freq-mismatch": "两轮频率集合不齐",
}


def _pair_key(base_label, retest_label):
    return "%s|%s" % (base_label, retest_label)


def auto_pair(base_points, retest_points, pos_tol):
    """自动配对。返回 (pairs, ambiguous, base_only, retest_only)。

    pairs: [{base_label, retest_label, method, dist}]
    ambiguous: [{label, side, candidates}] 多重匹配,保持无结论
    """
    retest_by_label = {p["label"]: p for p in retest_points}
    base_by_label = {p["label"]: p for p in base_points}
    pairs = []
    used_base, used_retest = set(), set()

    # 1) 点号对应
    for p in base_points:
        q = retest_by_label.get(p["label"])
        if q is not None:
            pairs.append({
                "base_label": p["label"], "retest_label": q["label"],
                "method": "label",
                "dist": round(math.hypot(p["x"] - q["x"], p["y"] - q["y"]), 3),
            })
            used_base.add(p["label"])
            used_retest.add(q["label"])

    # 2) 位置容差:仅当双方候选互唯一时配对,否则记多重匹配
    avail_b = [p for p in base_points if p["label"] not in used_base]
    avail_r = [p for p in retest_points if p["label"] not in used_retest]

    def candidates(p, pool):
        return [q for q in pool
                if math.hypot(p["x"] - q["x"], p["y"] - q["y"]) <= pos_tol]

    ambiguous = []
    for p in list(avail_b):
        cands = candidates(p, avail_r)
        if not cands:
            continue
        mutual = [q for q in cands
                  if len(candidates(q, avail_b)) == 1 and candidates(q, avail_b)[0] is p]
        if len(cands) == 1 and len(mutual) == 1:
            q = cands[0]
            pairs.append({
                "base_label": p["label"], "retest_label": q["label"],
                "method": "proximity",
                "dist": round(math.hypot(p["x"] - q["x"], p["y"] - q["y"]), 3),
            })
            used_base.add(p["label"])
            used_retest.add(q["label"])
            avail_b.remove(p)
            avail_r.remove(q)
        else:
            ambiguous.append({"label": p["label"], "side": "base",
                              "candidates": sorted(q["label"] for q in cands)})
    for q in avail_r:
        cands = candidates(q, avail_b)
        if cands:
            ambiguous.append({"label": q["label"], "side": "retest",
                              "candidates": sorted(c["label"] for c in cands)})

    base_only = [p["label"] for p in base_points if p["label"] not in used_base
                 and all(a["label"] != p["label"] for a in ambiguous)]
    retest_only = [p["label"] for p in retest_points if p["label"] not in used_retest
                   and all(a["label"] != p["label"] for a in ambiguous)]
    return pairs, ambiguous, sorted(base_only), sorted(retest_only)


def apply_overrides(pairs, ambiguous, base_only, retest_only, overrides,
                    base_labels, retest_labels):
    """把人工改配叠加到自动配对结果上。overrides 按时间顺序生效。

    每条 override: {base_label, retest_label(None=取消配对), note}
    返回新的 (pairs, ambiguous, base_only, retest_only);被改配拆散的
    自动配对成员回到未配对池。人工配对标记 method="manual"。
    """
    pairs = [dict(p) for p in pairs]
    ambiguous = [dict(a) for a in ambiguous]
    base_only = list(base_only)
    retest_only = list(retest_only)

    def drop_label(lst, label):
        lst[:] = [x for x in lst if x != label]

    def release(label, pool_only):
        """标签从配对/多重匹配中解除后,回到未配对池(若仍存在该轮)。"""
        if label and label not in pool_only:
            pool_only.append(label)

    for ov in overrides:
        bl, rl = ov["base_label"], ov.get("retest_label")
        # 拆除涉及这两个标签的既有配对,成员回到未配对池
        for pr in list(pairs):
            if bl in (pr["base_label"], pr["retest_label"]) or \
               (rl and rl in (pr["base_label"], pr["retest_label"])):
                pairs.remove(pr)
                release(pr["base_label"], base_only)
                release(pr["retest_label"], retest_only)
        ambiguous = [a for a in ambiguous
                     if a["label"] not in (bl, rl)]
        drop_label(base_only, bl)
        drop_label(retest_only, rl)
        if rl:
            drop_label(base_only, rl)
            drop_label(retest_only, bl)
            pairs.append({"base_label": bl, "retest_label": rl,
                          "method": "manual", "dist": None,
                          "note": ov["note"]})
        else:
            release(bl, base_only)
    return pairs, ambiguous, sorted(set(base_only)), sorted(set(retest_only))


def _freq_sets_compatible(bfreqs, rfreqs):
    """两轮频点集合能否一一对应(相对容差 FREQ_TOL)。返回 (ok, 对应表, 缺失说明)。"""
    used = set()
    mapping = []  # (base_freq, retest_freq)
    for bf in sorted(bfreqs):
        k = _close_freq({f: None for f in rfreqs if f not in used}, bf, tol=FREQ_TOL)
        if k is None:
            return False, [], "基准频点 %g Hz 在复测轮无对应" % bf
        used.add(k)
        mapping.append((bf, k))
    leftover = [f for f in rfreqs if f not in used]
    if leftover:
        return False, [], "复测轮多出频点 " + ",".join("%g" % f for f in sorted(leftover))
    return True, mapping, ""


def _field_margin(field, limits):
    """场强距阈值余量:正值=在限值内,数值=距最近边界的 dB 数。"""
    return round(min(field - limits["field_min"], limits["field_max"] - field), 3)


def _verdict(ok):
    return "ok" if ok else "fail"


def _migration(b_ok, r_ok):
    return "%s->%s" % (_verdict(b_ok), _verdict(r_ok))


def compute_pair(bp, rp, limits, method, dist, note=None):
    """计算一对已配对测点的逐频率差异与判定迁移。

    返回 pair 记录;status 为 "ok"(有结论)或 "noconclusion"(无结论,附 reason)。
    """
    pair = {
        "key": _pair_key(bp["label"], rp["label"]),
        "base_label": bp["label"], "retest_label": rp["label"],
        "x": bp["x"], "y": bp["y"], "rx": rp["x"], "ry": rp["y"],
        "method": method, "dist": dist,
        "status": "ok", "reason": "", "reason_code": "",
        "manual_note": note,
        "base_device": bp["device_id"], "base_calib": bp["calib_version"],
        "retest_device": rp["device_id"], "retest_calib": rp["calib_version"],
        "base_excluded": bool(bp["excluded"]), "retest_excluded": bool(rp["excluded"]),
    }

    def noconclusion(code, reason):
        pair.update(status="noconclusion", reason=reason, reason_code=code,
                    freqs=[], metrics=None, migration=None, degraded=False,
                    improved=False)
        return pair

    # 1) 测点数据有效性
    bad = []
    for tag, p in (("基准", bp), ("复测", rp)):
        if not p["valid"]:
            bad.append("%s轮 %s(%s)" % (tag, p["label"],
                                        ",".join(issue_label(i) for i in p["issues"])))
    if bad:
        return noconclusion("invalid-point", "测点数据无效:" + ";".join(bad))

    # 2) 校准依据兼容
    bcal, rcal = bp["calib_version"] or "未标注", rp["calib_version"] or "未标注"
    if bcal != rcal:
        return noconclusion("calib-mismatch",
                            "校准依据不兼容:基准 %s ≠ 复测 %s" % (bcal, rcal))

    # 3) 频率集合一致
    ok, mapping, why = _freq_sets_compatible(bp["freqs"], rp["freqs"])
    if not ok:
        return noconclusion("freq-mismatch", "两轮频率集合不齐:" + why)

    # 4) 逐频率计算
    freqs = []
    for bf, rf in mapping:
        b, r = bp["freqs"][bf], rp["freqs"][rf]
        b_snr, r_snr = b["field"] - b["noise"], r["field"] - r["noise"]
        b_fm, r_fm = _field_margin(b["field"], limits), _field_margin(r["field"], limits)
        b_sm, r_sm = round(b_snr - limits["snr_min"], 3), round(r_snr - limits["snr_min"], 3)
        freqs.append({
            "freq": bf,
            "base_field": b["field"], "retest_field": r["field"],
            "delta_field": round(r["field"] - b["field"], 3),
            "base_noise": b["noise"], "retest_noise": r["noise"],
            "delta_noise": round(r["noise"] - b["noise"], 3),
            "base_field_margin": b_fm, "retest_field_margin": r_fm,
            "delta_field_margin": round(r_fm - b_fm, 3),
            "base_snr_margin": b_sm, "retest_snr_margin": r_sm,
            "delta_snr_margin": round(r_sm - b_sm, 3),
            "base_field_ok": limits["field_min"] <= b["field"] <= limits["field_max"],
            "retest_field_ok": limits["field_min"] <= r["field"] <= limits["field_max"],
            "base_snr_ok": b_snr >= limits["snr_min"],
            "retest_snr_ok": r_snr >= limits["snr_min"],
        })

    # 点级指标:参考频点取配对频点中最接近 limits.ref_freq 者
    ref_bf, _ = min(mapping, key=lambda m: abs(m[0] - limits["ref_freq"]))
    b_ref = bp["freqs"][ref_bf]["field"]
    r_ref = rp["freqs"][[rf for bf2, rf in mapping if bf2 == ref_bf][0]]["field"]
    b_dev = max(abs(bp["freqs"][bf]["field"] - b_ref) for bf, _ in mapping)
    r_dev = max(abs(rp["freqs"][rf]["field"] - r_ref) for _, rf in mapping)
    b_snr_ref = b_ref - bp["freqs"][ref_bf]["noise"]
    r_snr_ref = r_ref - rp["freqs"][[rf for bf2, rf in mapping if bf2 == ref_bf][0]]["noise"]

    def metrics(field, snr, dev):
        return {"field": round(field, 3), "snr": round(snr, 3),
                "freq_dev": round(dev, 3)}

    b_m, r_m = metrics(b_ref, b_snr_ref, b_dev), metrics(r_ref, r_snr_ref, r_dev)
    b_field_ok = limits["field_min"] <= b_ref <= limits["field_max"]
    r_field_ok = limits["field_min"] <= r_ref <= limits["field_max"]
    b_snr_ok, r_snr_ok = b_snr_ref >= limits["snr_min"], r_snr_ref >= limits["snr_min"]
    b_dev_ok = b_dev <= limits["freq_dev_db"]
    r_dev_ok = r_dev <= limits["freq_dev_db"]
    b_all, r_all = all([b_field_ok, b_snr_ok, b_dev_ok]), all([r_field_ok, r_snr_ok, r_dev_ok])
    migration = {
        "field": _migration(b_field_ok, r_field_ok),
        "snr": _migration(b_snr_ok, r_snr_ok),
        "freq_dev": _migration(b_dev_ok, r_dev_ok),
        "overall": _migration(b_all, r_all),
    }
    pair.update(
        freqs=freqs,
        metrics={"ref_freq": ref_bf, "base": b_m, "retest": r_m,
                 "delta": {k: round(r_m[k] - b_m[k], 3) for k in b_m}},
        migration=migration,
        degraded=migration["overall"] == "ok->fail",
        improved=migration["overall"] == "fail->ok",
    )
    return pair


def degraded_clusters(pairs, cell_size, bounds):
    """成片退化聚类:退化对映射到网格,四连通聚类。n>=2 视为成片退化席位区。"""
    cs = float(cell_size)
    degraded = [p for p in pairs if p.get("degraded")]
    cells = {}
    for p in degraded:
        key = (int((p["x"] - bounds["min_x"]) // cs), int((p["y"] - bounds["min_y"]) // cs))
        cells.setdefault(key, []).append(p)
    seen = set()
    clusters = []
    for key in cells:
        if key in seen:
            continue
        stack, members = [key], []
        seen.add(key)
        while stack:
            k = stack.pop()
            members.extend(cells[k])
            i, j = k
            for nb in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if nb in cells and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        xs = [m["x"] for m in members]
        ys = [m["y"] for m in members]
        clusters.append({
            "x": round(sum(xs) / len(xs), 2),
            "y": round(sum(ys) / len(ys), 2),
            "n": len(members),
            "clustered": len(members) >= 2,
            "pairs": sorted(m["key"] for m in members),
            "labels": sorted({m["base_label"] for m in members} |
                             {m["retest_label"] for m in members}),
        })
    clusters.sort(key=lambda c: (-c["n"], c["y"], c["x"]))
    for idx, c in enumerate(clusters, 1):
        c["id"] = idx
    return clusters


def compute_comparison(base_points, retest_points, limits, bounds,
                       pos_tol=1.0, overrides=None):
    """复测对照主入口:配对 -> 逐对计算 -> 成片退化聚类,返回完整结果 dict。"""
    pairs0, ambiguous, base_only, retest_only = auto_pair(
        base_points, retest_points, float(pos_tol))
    if overrides:
        pairs0, ambiguous, base_only, retest_only = apply_overrides(
            pairs0, ambiguous, base_only, retest_only, overrides,
            {p["label"] for p in base_points}, {p["label"] for p in retest_points})
    bmap = {p["label"]: p for p in base_points}
    rmap = {p["label"]: p for p in retest_points}

    pairs = []
    for pr in pairs0:
        bp, rp = bmap.get(pr["base_label"]), rmap.get(pr["retest_label"])
        if bp is None or rp is None:
            continue
        pairs.append(compute_pair(bp, rp, limits, pr["method"], pr.get("dist"),
                                  note=pr.get("note")))
    pairs.sort(key=lambda p: (p["status"] != "ok", not p.get("degraded"),
                              p["base_label"]))

    clusters = degraded_clusters(pairs, limits["cell_size"], bounds)
    stats = {
        "paired": len(pairs),
        "concluded": sum(1 for p in pairs if p["status"] == "ok"),
        "noconclusion": sum(1 for p in pairs if p["status"] == "noconclusion"),
        "degraded": sum(1 for p in pairs if p.get("degraded")),
        "improved": sum(1 for p in pairs if p.get("improved")),
        "ambiguous": len(ambiguous),
        "base_only": len(base_only),
        "retest_only": len(retest_only),
        "degraded_clusters": sum(1 for c in clusters if c["clustered"]),
    }
    return {
        "pos_tol": float(pos_tol),
        "limits_snapshot": {k: limits[k] for k in
                            ("field_min", "field_max", "snr_min", "freq_dev_db",
                             "ref_freq", "cell_size")},
        "pairs": pairs,
        "ambiguous": ambiguous,
        "unpaired": {"base_only": base_only, "retest_only": retest_only},
        "clusters": clusters,
        "stats": stats,
    }
