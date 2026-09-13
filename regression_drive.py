"""驱动基准工作区回归检查(独立临时库,不影响 data/loop_survey.db)。

覆盖规则:
  1. 锚点 -> 分段时码映射 -> 测点归入有效电流区间,以冻结参考电流归一化
     (校正值 dB = 20*log10(I_ref/I_actual)),原始读数始终保留展示;
  2. 六类情形相应测点不得进入覆盖计算:映射倒退 / 采样断档 / 削波 / 过热 /
     校准量程不足 / 归一化幅度越限(另有超出锚点范围的无映射);
  3. 换绑锚点、保留异常段必须备注理由;每次成功变更生成并切换到新修订;
  4. 确认后锁定功放记录/场强记录/锚点/保留段/参数;三份导出取自同一确认结果,
     且能追到电流样本与换算值;重审修订号 +1;
  5. 复测对照只能引用已确认的驱动版本。

运行: PYTHONPATH=<flask所在> python3 regression_drive.py
"""
import io
import json
import os
import sys
import tempfile

os.environ["LOOP_DB"] = tempfile.mktemp(suffix=".db", prefix="loop_drive_regress_")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app  # noqa: E402

C = app.test_client()
FAILURES = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (" | " + str(detail) if detail else ""))
    if not cond:
        FAILURES.append(name)


VENUE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 30">'
         '<rect x="0" y="0" width="40" height="30" fill="#222"/></svg>')

r = C.post("/api/projects", data={"name": "驱动回归厅",
                                  "venue": (io.BytesIO(VENUE.encode()), "v.svg")},
           content_type="multipart/form-data")
assert r.status_code == 200, r.data
PID = r.get_json()["id"]

# 单频点、单点即可下结论,便于逐点核查覆盖计算
r = C.put("/api/projects/%d/limits" % PID, json={
    "field_min": -12.0, "field_max": 0.0, "uniformity_db": 6.0, "snr_min": 20.0,
    "freq_dev_db": 3.0, "ref_freq": 1000.0, "expected_freqs": [1000],
    "cell_size": 2.0, "influence_radius": 6.0, "min_points": 1})
assert r.status_code == 200, r.data


def new_survey(label="驱动校审"):
    return C.post("/api/projects/%d/drive-surveys" % PID,
                  json={"label": label}).get_json()["id"]


def upload_record(did, rows, hdr="time,current_a,clip,overheat\n"):
    text = hdr + "".join(",".join(str(v) for v in row) + "\n" for row in rows)
    return C.post("/api/drive-surveys/%d/record" % did,
                  data={"csv": (io.BytesIO(text.encode()), "rec.csv")},
                  content_type="multipart/form-data")


def upload_points(did, rows,
                  hdr="point_id,x,y,freq_hz,field_db,noise_db,time\n"):
    text = hdr + "".join(",".join(str(v) for v in row) + "\n" for row in rows)
    return C.post("/api/drive-surveys/%d/points" % did,
                  data={"csv": (io.BytesIO(text.encode()), "pts.csv")},
                  content_type="multipart/form-data")


def add_anchor(did, amp_t, field_t, note="锚点核对"):
    return C.post("/api/drive-surveys/%d/anchors" % did,
                  json={"amp_t": amp_t, "field_t": field_t, "note": note})


def state(did):
    return C.get("/api/drive-surveys/%d" % did).get_json()


def result(did):
    return state(did)["result"]


def pt(did, label):
    return next(p for p in result(did)["points"] if p["label"] == label)


def cell_at(did, cx, cy):
    return next(c for c in result(did)["cells"] if c["cx"] == cx and c["cy"] == cy)


def flat_record(tmax=600, step=60, current=2.0):
    return [(t, current, 0, 0) for t in range(0, tmax + 1, step)]


# ---------------------------------------------------------------- 1. 主流程:映射 + 归一化
did = new_survey("主流程")
rec = [(t, 4.0 if t == 300 else 2.0, 0, 0) for t in range(0, 601, 60)]
assert upload_record(did, rec).status_code == 200
assert upload_points(did, [
    ("P01", 8, 8, 1000, -6.0, -38.0, 120),
    ("P02", 30, 8, 1000, -5.0, -38.0, 300),
]).status_code == 200
# 本例 4 A 电流的校正值 -6.02 dB,放宽归一化幅度限值以演示其进入覆盖计算
r = C.put("/api/drive-surveys/%d/params" % did, json={"max_norm_db": 10.0})
assert r.status_code == 200, r.data
assert add_anchor(did, 0, 0).status_code == 200
r = add_anchor(did, 600, 600)
assert r.status_code == 200, r.data
res = result(did)
check("分段时码映射单调有效", res["mapping"]["ok"] is True
      and len(res["mapping"]["segments"]) == 1, res["mapping"])
p1, p2 = pt(did, "P01"), pt(did, "P02")
check("P01 归入电流区间:映射时刻 120 s、电流 2.0 A",
      p1["amp_t"] == 120 and abs(p1["current_a"] - 2.0) < 1e-9,
      (p1["amp_t"], p1["current_a"]))
check("P01 电流等于参考电流,校正值 0 dB,归一化=原始读数",
      abs(p1["correction_db"]) < 1e-9 and abs(p1["norm_ref_db"] + 6.0) < 1e-9,
      (p1["correction_db"], p1["norm_ref_db"]))
check("P02 电流 4 A,校正值 20*log10(2/4) = -6.02 dB",
      abs(p2["correction_db"] - (-6.0206)) < 0.001, p2["correction_db"])
check("P02 归一化 = 原始 -5.0 - 6.02 = -11.02 dB,原始读数保留",
      abs(p2["norm_ref_db"] + 11.0206) < 0.001 and p2["raw_ref_db"] == -5.0,
      (p2["norm_ref_db"], p2["raw_ref_db"]))
check("两测点均进入覆盖计算", p1["status"] == "ok" and p2["status"] == "ok")
c1 = cell_at(did, 4, 4)    # 含 P01(8,8)
c2 = cell_at(did, 15, 4)   # 含 P02(30,8)
check("覆盖网格使用归一化值(P01 格 -6.0 dB,合格)",
      c1["status"] == "ok" and abs(c1["field"] + 6.0) < 0.01, (c1["status"], c1["field"]))
check("覆盖网格使用归一化值(P02 格 -11.02 dB,仍在限值内)",
      c2["status"] == "ok" and abs(c2["field"] + 11.02) < 0.02,
      (c2["status"], c2["field"]))
check("统计:2 点全部合格", res["stats"]["n_ok"] == 2 and res["stats"]["n_excluded"] == 0,
      res["stats"])

# HH:MM:SS 时刻解析(功放与场强时钟可不同,锚点对齐)
did = new_survey("时钟格式")
upload_record(did, [("10:00:00", 2.0, 0, 0), ("10:10:00", 2.0, 0, 0)])
upload_points(did, [("T01", 8, 8, 1000, -6.0, -38.0, "09:05:00")])
# 两条样本相隔 10 分钟,放宽采样断档上限
assert C.put("/api/drive-surveys/%d/params" % did,
             json={"max_sample_gap_s": 900.0}).status_code == 200
add_anchor(did, "10:00:00", "09:00:00")
add_anchor(did, "10:10:00", "09:10:00")
p = pt(did, "T01")
check("HH:MM:SS 锚点映射:场强 09:05 -> 功放 10:05(36300 s)",
      p["amp_t"] == 36300 and p["status"] == "ok", (p["amp_t"], p["status"]))

# ---------------------------------------------------------------- 2. 六类排除
did = new_survey("六类排除")
rec = []
for t in range(0, 601, 60):
    cur, clip, oh = 2.0, 0, 0
    if t == 120:
        clip = 1                      # 削波
    if t == 180:
        oh = 1                        # 过热
    if t == 300:
        cur = 15.0                    # 超出校准量程上限 10 A
    if t == 360:
        cur = 4.0                     # 校正 -6.02 dB,超过归一化限值 3 dB
    if t == 480:
        continue                      # 420..540 采样断档(120 s > 60 s)
    rec.append((t, cur, clip, oh))
assert upload_record(did, rec).status_code == 200
assert upload_points(did, [
    ("A1", 8, 8, 1000, -6.0, -38.0, 120),
    ("A2", 16, 8, 1000, -6.0, -38.0, 180),
    ("A3", 24, 8, 1000, -6.0, -38.0, 300),
    ("A4", 32, 8, 1000, -6.0, -38.0, 360),
    ("A5", 8, 24, 1000, -6.0, -38.0, 480),
    ("A6", 16, 24, 1000, -6.0, -38.0, 60),
]).status_code == 200
add_anchor(did, 0, 0)
add_anchor(did, 600, 600)
res = result(did)
for label, code in [("A1", "clip"), ("A2", "overheat"), ("A3", "calib-range"),
                    ("A4", "norm-range"), ("A5", "sample-gap")]:
    p = pt(did, label)
    check("%s 被排除,原因 %s" % (label, code),
          p["status"] == "excluded" and code in p["reasons"], p["reasons"])
check("A6 正常进入覆盖计算", pt(did, "A6")["status"] == "ok")
check("统计:合格 1 / 排除 5", res["stats"]["n_ok"] == 1
      and res["stats"]["n_excluded"] == 5, res["stats"])
check("被排除测点不进入覆盖计算:其网格证据不足",
      cell_at(did, 4, 4)["status"] == "nodata"
      and cell_at(did, 4, 4)["n"] == 0, cell_at(did, 4, 4))
check("合格测点所在网格正常 ok", cell_at(did, 8, 12)["status"] == "ok",
      cell_at(did, 8, 12))
check("断档点原始读数仍展示(归一化值缺失但原始值在)",
      pt(did, "A5")["raw_ref_db"] == -6.0 and pt(did, "A5")["norm_ref_db"] is None)
check("采样断档被识别并汇总", any(g["t0"] == 420 and g["t1"] == 540
      for g in res["samples_summary"]["gaps"]), res["samples_summary"]["gaps"])

# 映射倒退:锚点方向相反
did = new_survey("映射倒退")
upload_record(did, flat_record())
upload_points(did, [("B1", 8, 8, 1000, -6.0, -38.0, 300)])
add_anchor(did, 600, 0, "首锚")
add_anchor(did, 0, 600, "方向记反的次锚")
p = pt(did, "B1")
check("映射倒退被识别,测点排除", p["status"] == "excluded"
      and "map-regression" in p["reasons"], p["reasons"])
check("映射整体标记无效", result(did)["mapping"]["ok"] is False)

# 超出锚点范围:无映射
did = new_survey("无映射")
upload_record(did, flat_record())
upload_points(did, [("C1", 8, 8, 1000, -6.0, -38.0, 900)])
add_anchor(did, 0, 0)
add_anchor(did, 600, 600)
p = pt(did, "C1")
check("超出锚点范围 -> 无映射排除", p["status"] == "excluded"
      and "no-mapping" in p["reasons"], p["reasons"])

# ---------------------------------------------------------------- 3. 换绑与保留段(备注 + 新修订)
did = new_survey("换绑与保留")
rec = [(t, 2.0, 1 if t == 120 else 0, 0) for t in range(0, 601, 60)]
upload_record(did, rec)
upload_points(did, [("K1", 8, 8, 1000, -6.0, -38.0, 120)])
r = C.post("/api/drive-surveys/%d/anchors" % did, json={"amp_t": 0, "field_t": 0})
check("绑定锚点无理由被拒(422)", r.status_code == 422, r.status_code)
check("拒绝后修订号仍为 1", state(did)["revision"] == 1)
r = add_anchor(did, 0, 0, "开演前对时")
check("绑定锚点成功并切到修订 2", r.status_code == 200
      and r.get_json()["revision"] == 2
      and r.get_json()["events"][0]["kind"] == "anchor"
      and r.get_json()["events"][0]["revision"] == 2, r.get_json().get("revision"))
r = add_anchor(did, 600, 600, "散场对时")
check("第二锚点再切到修订 3", r.get_json()["revision"] == 3)
check("削波点 K1 被排除", pt(did, "K1")["status"] == "excluded"
      and "clip" in pt(did, "K1")["reasons"])
check("削波点所在网格证据不足(未进入覆盖计算)",
      cell_at(did, 4, 4)["status"] == "nodata", cell_at(did, 4, 4)["status"])

r = C.post("/api/drive-surveys/%d/keeps" % did,
           json={"kind": "clip", "t0": 100, "t1": 140})
check("保留异常段无理由被拒(422)", r.status_code == 422, r.status_code)
check("拒绝后修订号仍为 3", state(did)["revision"] == 3)
r = C.post("/api/drive-surveys/%d/keeps" % did,
           json={"kind": "clip", "t0": 100, "t1": 140,
                 "note": "调光台瞬时过载,复测确认无失真"})
check("保留段成功并切到修订 4", r.status_code == 200
      and r.get_json()["revision"] == 4
      and r.get_json()["events"][0]["kind"] == "keep", r.get_json().get("revision"))
p = pt(did, "K1")
check("保留段豁免后 K1 进入覆盖计算,豁免原因留痕",
      p["status"] == "ok" and p["kept"] == ["clip"], (p["status"], p["kept"]))
check("豁免后网格恢复 ok", cell_at(did, 4, 4)["status"] == "ok")

kid = state(did)["keeps"][0]["id"]
r = C.delete("/api/drive-keeps/%d" % kid)
check("撤销保留段无理由被拒(422)", r.status_code == 422, r.status_code)
r = C.delete("/api/drive-keeps/%d" % kid, json={"note": "复测证据不足,收回豁免"})
check("撤销保留段成功并切到修订 5,K1 重新排除",
      r.status_code == 200 and r.get_json()["revision"] == 5
      and pt(did, "K1")["status"] == "excluded", r.get_json().get("revision"))

aid = state(did)["anchors"][0]["id"]
r = C.delete("/api/drive-anchors/%d" % aid, json={"note": "换绑到调音台时钟"})
check("删除锚点(换绑)成功并切到修订 6", r.status_code == 200
      and r.get_json()["revision"] == 6
      and r.get_json()["events"][0]["kind"] == "anchor-delete",
      r.get_json().get("revision"))
ev = state(did)["events"]
check("人工变更事件分别挂在新修订(不连续停留修订 1)",
      {(e["kind"], e["revision"]) for e in ev} >=
      {("anchor", 2), ("anchor", 3), ("keep", 4), ("keep-delete", 5),
       ("anchor-delete", 6)}, [(e["kind"], e["revision"]) for e in ev])
check("导入等非人工事件仍挂修订 1",
      all(e["revision"] == 1 for e in ev
          if e["kind"] in ("create", "import-record", "import-points")))

# ---------------------------------------------------------------- 4. 确认锁定与导出
did = new_survey("锁定导出")
check("缺功放记录不能确认", C.post("/api/drive-surveys/%d/confirm" % did)
      .status_code == 400)
upload_record(did, flat_record())
check("缺场强记录不能确认", C.post("/api/drive-surveys/%d/confirm" % did)
      .status_code == 400)
upload_points(did, [("L1", 8, 8, 1000, -6.0, -38.0, 120)])
add_anchor(did, 0, 0)
check("锚点不足 2 个不能确认", C.post("/api/drive-surveys/%d/confirm" % did)
      .status_code == 400)
add_anchor(did, 600, 600)
check("未确认不能导出", C.get("/api/drive-surveys/%d/export/recalc.json" % did)
      .status_code == 409)
r = C.post("/api/drive-surveys/%d/confirm" % did)
check("确认成功", r.status_code == 200 and r.get_json()["status"] == "confirmed",
      r.status_code)
check("确认后锁定来源功放记录", state(did)["source_record_id"])
for desc, rr in [
    ("导入功放记录", C.post("/api/drive-surveys/%d/record" % did,
        data={"csv": (io.BytesIO(b"time,current_a\n0,2.0\n"), "x.csv")},
        content_type="multipart/form-data")),
    ("导入场强记录", upload_points(did, [("L2", 8, 8, 1000, -6, -38, 60)])),
    ("绑定锚点", add_anchor(did, 60, 60, "x")),
    ("保留段", C.post("/api/drive-surveys/%d/keeps" % did,
        json={"kind": "clip", "t0": 0, "t1": 10, "note": "x"})),
    ("改参数", C.put("/api/drive-surveys/%d/params" % did,
        json={"ref_current_a": 3.0})),
]:
    check("确认后%s被锁定(409)" % desc, rr.status_code == 409, rr.status_code)

sv = C.get("/api/drive-surveys/%d/export/drive.svg" % did)
check("覆盖 SVG 可导出", sv.status_code == 200 and b"<svg" in sv.data)
cv = C.get("/api/drive-surveys/%d/export/points.csv" % did)
check("换算 CSV 可导出且含电流样本与换算值列",
      cv.status_code == 200 and b"correction_db" in cv.data
      and b"sample_lo_a" in cv.data and b"current_a" in cv.data, cv.status_code)
js = json.loads(C.get("/api/drive-surveys/%d/export/recalc.json" % did).data)
check("复算 JSON 含电流样本/锚点/逐行换算值",
      js["samples"] and js["anchors"] and js["result"]["rows"]
      and "correction_db" in js["result"]["rows"][0], js.keys())
check("复算 JSON 注明归一化公式", "20*log10" in js["normalization"])

r = C.post("/api/drive-surveys/%d/reopen" % did, json={"note": "复核后重审"})
check("重审成功且修订号 +1", r.status_code == 200
      and r.get_json()["revision"] == state(did)["revision"] > 1,
      r.get_json().get("revision"))
check("重审后导出再次锁定(409)",
      C.get("/api/drive-surveys/%d/export/drive.svg" % did).status_code == 409)

# ---------------------------------------------------------------- 5. 复测对照(仅已确认版本)
b1 = new_survey("基准轮")
upload_record(b1, flat_record())
upload_points(b1, [("Q1", 8, 8, 1000, -6.0, -38.0, 120)])
add_anchor(b1, 0, 0)
add_anchor(b1, 600, 600)
b2 = new_survey("复测轮")
upload_record(b2, flat_record(current=2.5))
upload_points(b2, [("Q1", 8, 8, 1000, -6.0, -38.0, 120)])
add_anchor(b2, 0, 0)
add_anchor(b2, 600, 600)

r = C.post("/api/projects/%d/drive-compares" % PID,
           json={"base_survey_id": b1, "retest_survey_id": b2})
check("草稿版本不能建立对照(409)", r.status_code == 409, r.status_code)
assert C.post("/api/drive-surveys/%d/confirm" % b1).status_code == 200
r = C.post("/api/projects/%d/drive-compares" % PID,
           json={"base_survey_id": b1, "retest_survey_id": b2})
check("单方确认仍不能建立对照(409)", r.status_code == 409, r.status_code)
assert C.post("/api/drive-surveys/%d/confirm" % b2).status_code == 200
r = C.post("/api/projects/%d/drive-compares" % PID,
           json={"base_survey_id": b1, "retest_survey_id": b2})
check("双方确认后对照成功", r.status_code == 200, r.status_code)
cmp_res = r.get_json()["result"]
check("对照配对 1 点", cmp_res["stats"]["n_pairs"] == 1, cmp_res["stats"])
# 复测轮电流 2.5 A:校正 20*log10(2/2.5) = -1.938 dB,差值即校正差
check("对照差值 = 归一化校正差(-1.94 dB)",
      abs(cmp_res["pairs"][0]["delta_db"] - (-1.938)) < 0.001,
      cmp_res["pairs"][0]["delta_db"])
check("对照保留两侧原始读数与电流",
      cmp_res["pairs"][0]["base_raw_db"] == -6.0
      and abs(cmp_res["pairs"][0]["retest_current_a"] - 2.5) < 1e-9,
      cmp_res["pairs"][0])
cid = r.get_json()["id"]
check("对照详情可回查", C.get("/api/drive-compares/%d" % cid).status_code == 200)
check("对照列表可查", any(c["id"] == cid for c in
      C.get("/api/projects/%d/drive-compares" % PID).get_json()))

# ---------------------------------------------------------------- 6. 导出追溯(确认结果)
cv_text = C.get("/api/drive-surveys/%d/export/points.csv" % b2).data.decode("utf-8-sig")
check("CSV 逐行可追到电流样本与换算值",
      "2.500" in cv_text and "-1.94" in cv_text and "sample_lo_t_s" in cv_text,
      cv_text.splitlines()[1] if cv_text else "")
js = json.loads(C.get("/api/drive-surveys/%d/export/recalc.json" % b2).data)
check("JSON 样本与确认时一致(2.5 A)",
      all(abs(s["current"] - 2.5) < 1e-9 for s in js["samples"]), js["samples"][:2])
check("JSON 逐点含原始与归一化读数",
      js["result"]["points"][0]["raw_ref_db"] == -6.0
      and abs(js["result"]["points"][0]["norm_ref_db"] + 7.938) < 0.001,
      js["result"]["points"][0]["norm_ref_db"])

# ---------------------------------------------------------------- 7. 缺陷1:修订独立快照
did = new_survey("快照")
rec = [(t, 2.0, 1 if t == 120 else 0, 0) for t in range(0, 601, 60)]
upload_record(did, rec)
upload_points(did, [("S1", 8, 8, 1000, -6.0, -38.0, 120)])
add_anchor(did, 0, 0, "首锚")                       # 修订 2
add_anchor(did, 600, 600, "次锚")                   # 修订 3(S1 削波排除)
r = C.post("/api/drive-surveys/%d/keeps" % did,
           json={"kind": "clip", "t0": 100, "t1": 140, "note": "瞬时过载已复核"})
assert r.status_code == 200 and r.get_json()["revision"] == 4

snaps = state(did)["snapshots"]
check("每次重算按修订独立保存快照(1..4)",
      [s["revision"] for s in snaps] == [4, 3, 2, 1],
      [s["revision"] for s in snaps])
r3 = C.get("/api/drive-surveys/%d/revisions/3" % did).get_json()
check("修订 3 快照:无保留段,S1 削波排除",
      r3["result"]["keeps"] == []
      and r3["result"]["points"][0]["status"] == "excluded"
      and "clip" in r3["result"]["points"][0]["reasons"],
      r3["result"]["points"][0]["status"])
check("修订 3 快照锚点为当时两条", len(r3["result"]["anchors"]) == 2)
r4 = C.get("/api/drive-surveys/%d/revisions/4" % did).get_json()
check("修订 4 快照:保留段生效,S1 豁免进入覆盖",
      len(r4["result"]["keeps"]) == 1
      and r4["result"]["points"][0]["status"] == "ok"
      and r4["result"]["points"][0]["kept"] == ["clip"],
      r4["result"]["points"][0]["kept"])
check("历史修订三份导出可用",
      all(C.get("/api/drive-surveys/%d/revisions/3/export/%s" % (did, k)).status_code
          == 200 for k in ("drive.svg", "points.csv", "recalc.json")))
js3 = json.loads(C.get("/api/drive-surveys/%d/revisions/3/export/recalc.json" % did).data)
check("修订 3 导出内容即当时结果(排除、无保留段)",
      js3["result"]["points"][0]["status"] == "excluded" and js3["keeps"] == []
      and js3["survey"]["revision"] == 3)
check("修订 3 导出只含该修订及之前的事件",
      all(e["revision"] <= 3 for e in js3["events"]),
      [e["revision"] for e in js3["events"]])
check("当前修订未确认时其导出仍 409",
      C.get("/api/drive-surveys/%d/revisions/4/export/recalc.json" % did).status_code == 409)

assert C.post("/api/drive-surveys/%d/confirm" % did).status_code == 200
check("确认后当前修订导出 200 且与快照同源",
      C.get("/api/drive-surveys/%d/revisions/4/export/recalc.json" % did).status_code == 200
      and json.loads(C.get("/api/drive-surveys/%d/export/recalc.json" % did).data)
          ["result"]["points"][0]["kept"] == ["clip"])

# 重审 + 撤销保留段:修订 4 的确认快照不得被覆盖
assert C.post("/api/drive-surveys/%d/reopen" % did, json={"note": "复核"}).status_code == 200
kid = state(did)["keeps"][0]["id"]
r = C.delete("/api/drive-keeps/%d" % kid, json={"note": "证据不足,收回豁免"})
assert r.status_code == 200 and r.get_json()["revision"] == 6
check("当前修订(6)S1 重新排除", pt(did, "S1")["status"] == "excluded")
r4b = C.get("/api/drive-surveys/%d/revisions/4" % did).get_json()
check("变更后修订 4 快照仍是当时结果(S1 豁免 ok,未被覆盖)",
      r4b["result"]["points"][0]["status"] == "ok"
      and r4b["result"]["points"][0]["kept"] == ["clip"]
      and len(r4b["result"]["keeps"]) == 1,
      r4b["result"]["points"][0]["status"])
check("修订 4 快照电流样本仍在(可追溯)", len(r4b["samples"]) == 11)
check("重审本身不产生新计算:修订 5 无快照(404)",
      C.get("/api/drive-surveys/%d/revisions/5" % did).status_code == 404)
check("快照列表含新修订 6 且保留历史 4",
      [s["revision"] for s in state(did)["snapshots"]] == [6, 4, 3, 2,1],
      [s["revision"] for s in state(did)["snapshots"]])
check("历史修订 4 导出仍可用(当前修订未确认不影响)",
      C.get("/api/drive-surveys/%d/revisions/4/export/points.csv" % did).status_code == 200)
check("不存在的修订快照 404",
      C.get("/api/drive-surveys/%d/revisions/99" % did).status_code == 404)

# ---------------------------------------------------------------- 8. 缺陷2:断档保留一致性
did = new_survey("断档保留")
rec = [(t, 3.0 if t >= 540 else 2.0, 0, 0) for t in range(0, 601, 60) if t != 480]
upload_record(did, rec)
upload_points(did, [
    ("G1", 8, 8, 1000, -6.0, -38.0, 480),    # 断档 420..540 内
    ("G2", 16, 8, 1000, -6.0, -38.0, 900),   # 记录范围之外(>600)
])
add_anchor(did, 0, 0, "首锚")
add_anchor(did, 1200, 1200, "次锚")
g1, g2 = pt(did, "G1"), pt(did, "G2")
check("保留前 G1 断档排除,无电流与归一化值",
      g1["status"] == "excluded" and "sample-gap" in g1["reasons"]
      and g1["current_a"] is None and g1["norm_ref_db"] is None,
      (g1["status"], g1["current_a"]))
check("保留前 G1 网格证据不足", cell_at(did, 4, 4)["status"] == "nodata")
check("保留前两点均排除", result(did)["stats"]["n_excluded"] == 2)

r = C.post("/api/drive-surveys/%d/keeps" % did,
           json={"kind": "sample-gap", "t0": 400, "t1": 560,
                 "note": "传输丢包,两侧样本趋势平稳,接受跨档插值"})
assert r.status_code == 200
g1 = pt(did, "G1")
check("断档保留后 G1 进入覆盖计算且豁免留痕",
      g1["status"] == "ok" and g1["kept"] == ["sample-gap"], (g1["status"], g1["kept"]))
check("保留后 G1 有跨档插值电流(2.0→3.0 之间 480s 处 = 2.5 A)",
      g1["current_a"] is not None and abs(g1["current_a"] - 2.5) < 1e-9,
      g1["current_a"])
check("保留后 G1 有归一化值(校正 20*log10(2/2.5) = -1.94 dB)",
      g1["correction_db"] is not None
      and abs(g1["correction_db"] - (-1.9382)) < 0.001
      and abs(g1["norm_ref_db"] - (-7.9382)) < 0.001,
      (g1["correction_db"], g1["norm_ref_db"]))
c = cell_at(did, 4, 4)
check("保留后 G1 进入对应网格(ok,场强为归一化值)",
      c["status"] == "ok" and abs(c["field"] + 7.94) < 0.02, (c["status"], c["field"]))

r = C.post("/api/drive-surveys/%d/keeps" % did,
           json={"kind": "sample-gap", "t0": 800, "t1": 1000,
                 "note": "尝试保留记录范围外的断档"})
assert r.status_code == 200
g2 = pt(did, "G2")
check("记录范围外无可换算电流:保留段也不能豁免,仍禁入且原因明确",
      g2["status"] == "excluded" and g2["reasons"] == ["sample-gap"]
      and g2["current_a"] is None and g2["norm_ref_db"] is None,
      (g2["status"], g2["reasons"]))
check("G2 不得标为 ok 或进入覆盖计算(其网格仍证据不足)",
      cell_at(did, 8, 4)["status"] == "nodata", cell_at(did, 8, 4)["status"])
check("统计一致:仅 G1 合格,G2 仍排除",
      result(did)["stats"]["n_ok"] == 1 and result(did)["stats"]["n_excluded"] == 1,
      result(did)["stats"])

os.unlink(os.environ["LOOP_DB"])
print()
if FAILURES:
    print("FAILED:", len(FAILURES), "项")
    sys.exit(1)
print("ALL DRIVE REGRESSION CHECKS PASSED")
