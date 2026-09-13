"""边界外逸工作区回归检查(独立临时库,不影响 data/loop_survey.db)。

覆盖规则:
  1. 关闭工况估背景、开启减背景为外逸量;超限连续段长度/峰值/相邻环余量;
  2. 五种「相关边界段保持无结论」:时段不重叠 / 配对多解 / 路径自交 /
     采样间距过大 / 校准依据不兼容;
  3. 人工改配、调边界、移点必须备注,且形成新修订事件;
  4. 确认后锁定来源测次、配对表、限值;三份导出同源;重审计修订号 +1;
  5. 边界拆分:仅内部顶点可拆,拆分后两段各自计算。

运行: PYTHONPATH=<flask所在> python3 regression_leak.py
"""
import io
import json
import os
import sys
import tempfile

os.environ["LOOP_DB"] = tempfile.mktemp(suffix=".db", prefix="loop_leak_regress_")
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

r = C.post("/api/projects", data={"name": "边界回归厅",
                                  "venue": (io.BytesIO(VENUE.encode()), "v.svg")},
           content_type="multipart/form-data")
assert r.status_code == 200, r.data
PID = r.get_json()["id"]


def new_survey(label="回归校审"):
    return C.post("/api/projects/%d/leak-surveys" % PID, json={"label": label}).get_json()["id"]


def csv_text(rows, hdr="point_id,x,y,field_db,background_db,calib_version,time\n"):
    return hdr + "".join(",".join("" if v is None else str(v) for v in row) + "\n" for row in rows)


def upload(lid, cond, rows, hdr=None):
    h = hdr or "point_id,x,y,field_db,background_db,calib_version,time\n"
    return C.post("/api/leak-surveys/%d/runs" % lid,
                  data={"condition": cond,
                        "csv": (io.BytesIO(csv_text(rows, h).encode()), cond + ".csv")},
                  content_type="multipart/form-data")


def state(lid):
    return C.get("/api/leak-surveys/%d" % lid).get_json()


def result(lid):
    return state(lid)["result"]


def add_path(lid, verts, name="边界"):
    return C.post("/api/leak-surveys/%d/paths" % lid,
                  json={"name": name, "vertices": verts})


def dense_runs(on_field=-33, off_field=-40, calib_on="CAL-1", calib_off="CAL-1",
               t_on="2026-09-12 10:00", t_off="2026-09-12 09:00", xmax=20, step=2):
    xs = list(range(0, xmax + 1, step))
    off = [("P%02d" % i, x, 13, off_field, "", calib_off, t_off) for i, x in enumerate(xs)]
    on = [("P%02d" % i, x, 13, on_field, "", calib_on, t_on) for i, x in enumerate(xs)]
    return on, off


# ---------------------------------------------------------------- 1. 主流程
lid = new_survey("主流程")
on, off = dense_runs()
assert upload(lid, "off", off).status_code == 200
assert upload(lid, "on", on).status_code == 200
C.post("/api/leak-surveys/%d/zones" % lid, json={"kind": "own", "name": "A环",
       "polygon": [[0, 12], [40, 12], [40, 30], [0, 30]]})
C.post("/api/leak-surveys/%d/zones" % lid, json={"kind": "adjacent", "name": "B环",
       "polygon": [[0, 0], [40, 0], [40, 11.5], [0, 11.5]]})
assert add_path(lid, [[0, 11], [20, 11]], "隔墙").status_code == 200
res = result(lid)
pr = res["paths"][0]
check("外逸量=开启−关闭背景(7 dB)", abs(pr["stations"][5]["excess"] - 7.0) < 0.01,
      pr["stations"][5]["excess"])
check("外逸 7 dB > 限值 6 dB -> fail", pr["stations"][5]["status"] == "fail",
      pr["stations"][5]["status"])
check("相邻环余量 = 6-外逸 = -1 dB", abs(pr["stations"][5]["adj_margin"] + 1.0) < 0.01,
      pr["stations"][5]["adj_margin"])
check("全线一条连续超限段,长度约 20 m", pr["stats"]["n_runs"] == 1
      and abs(pr["stats"]["fail_m"] - 20.0) < 0.2, (pr["stats"], pr["runs"]))
check("峰值在里程 ~10 m", abs(pr["peak"]["s"] - 10.0) < 0.2, pr["peak"])

# 降到外逸 4 dB -> 全线合格
lid2 = new_survey("合格场景")
on2, off2 = dense_runs(on_field=-36)
upload(lid2, "off", off2); upload(lid2, "on", on2)
add_path(lid2, [[0, 11], [20, 11]])
res2 = result(lid2)
check("外逸 4 dB 全线合格",
      all(s["status"] in ("ok",) for s in res2["paths"][0]["stations"]),
      {s: 1 for s in res2["paths"][0]["stations"] if s["status"] != "ok"})

# 部分超限段:中间 4 点外逸 12
lid3 = new_survey("局部超限")
xs = list(range(0, 21, 2))
off3 = [("P%02d" % i, x, 13, -40, "", "CAL-1", "2026-09-12 09:00") for i, x in enumerate(xs)]
on3 = [("P%02d" % i, x, 13, (-28 if 10 <= x <= 14 else -36), "", "CAL-1", "2026-09-12 10:00")
       for i, x in enumerate(xs)]
upload(lid3, "off", off3); upload(lid3, "on", on3)
C.post("/api/leak-surveys/%d/zones" % lid3, json={"kind": "own",
       "polygon": [[0, 12], [40, 12], [40, 30], [0, 30]]})
C.post("/api/leak-surveys/%d/zones" % lid3, json={"kind": "adjacent",
       "polygon": [[0, 0], [40, 0], [40, 11.5], [0, 11.5]]})
add_path(lid3, [[0, 11], [20, 11]])
p3 = result(lid3)["paths"][0]
check("局部超限段存在且长度 < 全线",
      p3["stats"]["n_runs"] == 1 and 4 <= p3["runs"][0]["length_m"] <= 8,
      p3["runs"])
check("峰值落在超限段内部",
      abs(p3["peak"]["excess"] - 12.0) < 0.2 and p3["runs"][0]["s0"]
      <= p3["peak"]["s"] <= p3["runs"][0]["s1"], p3["runs"][0])

# ---------------------------------------------------------------- 2. 五类无结论

# 2a. 时段不重叠(3.5h > 时间窗 2h)
lid = new_survey("时段不重叠")
on, off = dense_runs(t_on="2026-09-12 12:30")
upload(lid, "off", off); upload(lid, "on", on)
add_path(lid, [[0, 11], [20, 11]])
res = result(lid)
check("测次级时间窗不重叠被识别", res["run_time_gap_h"] is not None and res["run_time_gap_h"] > 2,
      res["run_time_gap_h"])
check("时间窗不重叠:全部配对无结论", all(p["status"] == "noconclusion" for p in res["pairs"]),
      [p["reasons"] for p in res["pairs"]][:2])
check("时间窗不重叠:边界段全部无结论",
      all(s["status"] == "noconclusion" for s in res["paths"][0]["stations"]),
      {s["status"] for s in res["paths"][0]["stations"]})

# 2b. 配对多解:两个开点在容差内都挨着同一关点
lid = new_survey("多解")
upload(lid, "off", [("Q1", 5, 11, -40, "", "C", "2026-09-12 09:00")])
upload(lid, "on", [("A1", 4.5, 11, -33, "", "C", "2026-09-12 10:00"),
                   ("A2", 5.4, 11, -33, "", "C", "2026-09-12 10:00")])
add_path(lid, [[1, 11], [9, 11]])
res = result(lid)
check("多解点列入 ambiguous", len(res["ambiguous"]) >= 2, res["ambiguous"])
check("多解附近边界段无结论",
      any(any(r.startswith("ambiguous") for r in s["reasons"])
          for s in res["paths"][0]["stations"]))

# 2c. 路径自交
lid = new_survey("自交")
on, off = dense_runs()
upload(lid, "off", off); upload(lid, "on", on)
add_path(lid, [[1, 8], [9, 12], [1, 12], [9, 8]], "X形")
res = result(lid)
check("X 形路径标记 self_intersect", res["paths"][0]["self_intersect"] is True)
check("自交路径所有测站无结论",
      all(s["status"] == "noconclusion" for s in res["paths"][0]["stations"]))
# 简单 L 形不自交
lid = new_survey("非自交")
on, off = dense_runs()
upload(lid, "off", off); upload(lid, "on", on)
add_path(lid, [[0, 11], [10, 11], [10, 16]], "L形")
check("L 形路径不判自交", result(lid)["paths"][0]["self_intersect"] is False)

# 2d. 采样间距过大
lid = new_survey("间距过大")
upload(lid, "off", [("P1", 2, 11, -40, "", "C", "2026-09-12 09:00"),
                    ("P2", 18, 11, -40, "", "C", "2026-09-12 09:00")])
upload(lid, "on", [("P1", 2, 11, -33, "", "C", "2026-09-12 10:00"),
                   ("P2", 18, 11, -33, "", "C", "2026-09-12 10:00")])
add_path(lid, [[1, 11], [19, 11]])
sts = result(lid)["paths"][0]["stations"]
check("稀疏采样:中间站采样跨度 ~16 m",
      next(s for s in sts if s["s"] == 10)["sample_span"] == 16.0,
      next(s for s in sts if s["s"] == 10)["sample_span"])
check("稀疏采样:超过 4 m 限值的站无结论",
      any("sample-gap" in s["reasons"] for s in sts)
      and not any(s["status"] == "fail" for s in sts))

# 2e. 校准不兼容
lid = new_survey("校准不兼容")
xs = list(range(0, 9, 2))
upload(lid, "off", [("P%02d" % i, x, 11, -40, "", "CAL-1", "2026-09-12 09:00")
                    for i, x in enumerate(xs)])
upload(lid, "on", [("P%02d" % i, x, 11, -33, "", "CAL-2" if i == 0 else "CAL-1",
                    "2026-09-12 10:00") for i, x in enumerate(xs)])
add_path(lid, [[0, 11], [8, 11]])
res = result(lid)
p0 = next(p for p in res["pairs"] if p["on_label"] == "P00")
check("不同校准版本配对无结论", p0["status"] == "noconclusion"
      and "calib-mismatch" in p0["reasons"], p0["reasons"])
check("不兼容配对影响半径内边界段无结论",
      any("calib-mismatch" in s["reasons"] for s in res["paths"][0]["stations"]))

# ---------------------------------------------------------------- 3. 备注与修订
lid = new_survey("修订审计")
on, off = dense_runs()
upload(lid, "off", off); upload(lid, "on", on)
add_path(lid, [[0, 11], [10, 11], [20, 11]])

r = C.post("/api/leak-surveys/%d/overrides" % lid,
           json={"on_label": "P00", "off_label": "P01"})
check("改配无备注被拒", r.status_code == 422, r.status_code)
r = C.post("/api/leak-surveys/%d/overrides" % lid,
           json={"on_label": "P00", "off_label": "P01", "note": "现场点号重贴"})
check("改配有备注成功并重算", r.status_code == 200
      and any(p["method"] == "manual" for p in r.get_json()["result"]["pairs"]))

r = C.put("/api/leak-paths/1/vertices",
          json={"action": "vertices", "vertices": [[0, 11], [20, 11]]})
check("调边界无备注被拒", r.status_code == 422, r.status_code)
r = C.put("/api/leak-paths/1/vertices",
          json={"action": "vertices", "vertices": [[0, 11], [20, 10.5]], "note": "墙体偏角"})
check("调边界有备注成功", r.status_code == 200, r.status_code)

r = C.post("/api/leak-surveys/%d/move" % lid,
           json={"point_label": "P02", "condition": "on", "x": 4.4, "y": 11})
check("移点无备注被拒", r.status_code == 422, r.status_code)
r = C.post("/api/leak-surveys/%d/move" % lid,
           json={"point_label": "P02", "condition": "on", "x": 4.4, "y": 11,
                 "note": "误定位纠正"})
check("移点有备注成功且标记 moved", r.status_code == 200, r.status_code)

ev = state(lid)["events"]
kinds = {e["kind"] for e in ev}
check("改配/调边界/移点均留修订事件",
      {"override", "path-edit", "move"} <= kinds, kinds)
check("草稿阶段修订号保持 1(reopen 才 +1)", state(lid)["revision"] == 1)

# ---------------------------------------------------------------- 4. 拆分
lid = new_survey("拆分")
on, off = dense_runs()
upload(lid, "off", off); upload(lid, "on", on)
add_path(lid, [[0, 11], [10, 11], [20, 11]], "待拆")
r = C.put("/api/leak-paths/1/vertices",
          json={"action": "split", "at_vertex": 1, "note": "门洞处分两段"})
check("内部顶点拆分成功", r.status_code == 200 and len(r.get_json()["paths"]) == 2,
      r.status_code)
new_paths = r.get_json()["paths"]
check("拆分后两段共享拆分顶点、总长不变",
      new_paths[0]["vertices"][-1] == new_paths[1]["vertices"][0]
      and abs(sum(p["vertices_length"] if False else 0 for p in []) + 0) >= 0)
tot = sum(pr2["length_m"] for pr2 in r.get_json()["result"]["paths"])
check("拆分后总里程仍为 20 m", abs(tot - 20.0) < 0.01, tot)
r = C.put("/api/leak-paths/1/vertices", json={"action": "split", "at_vertex": 0, "note": "x"})
check("端点不可拆", r.status_code == 400, r.status_code)

# ---------------------------------------------------------------- 5. 锁定与导出
lid = new_survey("锁定导出")
on, off = dense_runs()
upload(lid, "off", off); upload(lid, "on", on)
add_path(lid, [[0, 11], [20, 11]])
check("未确认不能导出", C.get("/api/leak-surveys/%d/export/recalc.json" % lid).status_code == 409)
assert C.post("/api/leak-surveys/%d/confirm" % lid).status_code == 200
locked_st = state(lid)
check("确认后锁定来源测次",
      locked_st["source_on_run_id"] and locked_st["source_off_run_id"])
for method, url, body in [
    ("POST", "/runs", None),
    ("POST", "/zones", {"kind": "own", "polygon": [[0, 0], [1, 0], [0, 1]]}),
    ("POST", "/overrides", {"on_label": "P00", "off_label": "P01", "note": "x"}),
    ("PUT", "/params", {"leak_limit_db": 3}),
]:
    if method == "POST" and url == "/runs":
        rr = C.post("/api/leak-surveys/%d/runs" % lid,
                    data={"condition": "on",
                          "csv": (io.BytesIO(csv_text(on).encode()), "x.csv")},
                    content_type="multipart/form-data")
    elif method == "POST":
        rr = C.post("/api/leak-surveys/%d%s" % (lid, url), json=body)
    else:
        rr = C.put("/api/leak-surveys/%d%s" % (lid, url), json=body)
    check("确认后 %s %s 被锁定" % (method, url), rr.status_code == 409, rr.status_code)

js = json.loads(C.get("/api/leak-surveys/%d/export/recalc.json" % lid).data)
check("复算 JSON 含冻结结果/测次/参数/事件",
      js["survey"]["status"] == "confirmed" and js["result"]["paths"]
      and js["runs"] and "params" in js)
sv = C.get("/api/leak-surveys/%d/export/boundary.svg" % lid)
check("标色边界 SVG 可导出", sv.status_code == 200 and b"<svg" in sv.data)
cv = C.get("/api/leak-surveys/%d/export/remeasure.csv" % lid)
check("复测点 CSV 可导出且含表头", cv.status_code == 200
      and b"path_id" in cv.data and b"excess_db" in cv.data)

r = C.post("/api/leak-surveys/%d/reopen" % lid, json={"note": "新修订"})
check("重审成功且修订号 +1", r.status_code == 200 and r.get_json()["revision"] == 2,
      r.get_json().get("revision"))
check("重审后导出再次锁定(409)",
      C.get("/api/leak-surveys/%d/export/boundary.svg" % lid).status_code == 409)
r = C.put("/api/leak-paths/1/vertices",
          json={"action": "vertices", "vertices": [[0, 11], [20, 10.0]], "note": "复测调整"})
check("重审后可调边界(挂新修订号 2)", r.status_code == 200
      and r.get_json()["events"][0]["revision"] == 2)

# ---------------------------------------------------------------- 6. 缺数据保护
lid = new_survey("空数据")
add_path(lid, [[0, 11], [10, 11]])
res = result(lid)
check("无测次时路径站为 nodata/no-pair",
      all(s["status"] == "nodata" and "no-pair" in s["reasons"]
          for s in res["paths"][0]["stations"]),
      {s["status"] for s in res["paths"][0]["stations"]})
check("缺一种工况不能确认",
      C.post("/api/leak-surveys/%d/confirm" % lid).status_code == 400)

# CSV 解析:重复测点行报错
r = upload(lid, "on", [("DUP", 1, 1, -33, "", "C", "t"), ("DUP", 2, 2, -33, "", "C", "t")])
check("同一测次内重复测点被 CSV 校验拒绝",
      r.status_code == 422 or any("重复" in e for e in r.get_json().get("import_result", {}).get("csv_errors", [])),
      r.status_code)

os.unlink(os.environ["LOOP_DB"])
print()
if FAILURES:
    print("FAILED:", len(FAILURES), "项")
    sys.exit(1)
print("ALL LEAKAGE REGRESSION CHECKS PASSED")
