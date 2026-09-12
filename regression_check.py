"""回归检查(独立临时库,不影响 data/loop_survey.db):

缺陷 1 反例:缺频点的 MISS 测点虽无效,其影响半径内的网格曾返回 ok。
  -> 现在这些网格必须全部为 noconclusion,即使附近有 >=3 个有效测点;
     远离无效测点的有效测点簇仍应正常得出 ok。
缺陷 2:复算 JSON 的 manual_decisions 按版本隔离。
  -> v1 的导出不得包含 v2 才产生的决定,v2 也只能看到本版记录。

运行: PYTHONPATH=<flask所在> python3 regression_check.py
"""
import io
import json
import os
import sys
import tempfile

os.environ["LOOP_DB"] = tempfile.mktemp(suffix=".db", prefix="loop_regress_")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app  # noqa: E402  (需在设置 LOOP_DB 后导入)

C = app.test_client()
FREQS = [100, 500, 1000, 2000, 4000, 5000]
FAILURES = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (" | " + str(detail) if detail else ""))
    if not cond:
        FAILURES.append(name)


def csv_text(rows):
    out = io.StringIO()
    out.write("point_id,x,y,freq_hz,field_db,noise_db,device_id,calib_version\n")
    for r in rows:
        out.write(",".join(str(v) for v in r) + "\n")
    return out.getvalue()


def make_rows(label, x, y, freqs=FREQS, field=-6.0, noise=-38.0, calib="CAL-A"):
    return [[label, x, y, f, field, noise, "FSM-1", calib] for f in freqs]


def import_csv(pid, label, text):
    return C.post("/api/projects/%d/import" % pid,
                  data={"label": label, "csv": (io.BytesIO(text.encode()), label + ".csv")},
                  content_type="multipart/form-data")


def recalc(vid):
    r = C.get("/api/versions/%d/export/recalc.json" % vid)
    assert r.status_code == 200, r.status_code
    return json.loads(r.data)


VENUE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">'
         '<rect x="0" y="0" width="20" height="20" fill="#222"/></svg>')

# ---------------------------------------------------------------- 准备项目
r = C.post("/api/projects", data={"name": "回归剧场", "venue": (io.BytesIO(VENUE.encode()), "v.svg")},
           content_type="multipart/form-data")
assert r.status_code == 200, r.data
PID = r.get_json()["id"]

# 缺频点 M(10,10) 四周 4 个有效测点(距离 3,影响半径 6 内);
# 远处 (16..18,16..18) 再放 4 个有效测点,远离 M(约 10m)应正常 ok。
rows = []
rows += make_rows("M-BAD", 10, 10, freqs=[100, 500, 1000, 2000])  # 缺 4000/5000
for label, x, y in [("A", 7, 10), ("B", 13, 10), ("C", 10, 7), ("D", 10, 13),
                    ("E", 16, 16), ("F", 18, 16), ("G", 16, 18), ("H", 18, 18)]:
    rows += make_rows(label, x, y)
s1 = import_csv(PID, "v1-空场", csv_text(rows))
assert s1.status_code == 200, s1.data
V1 = s1.get_json()["version"]["id"]

# ---------------------------------------------------------------- 缺陷 1:MISS 反例
state = C.get("/api/projects/%d/state" % PID).get_json()
bad = [p for p in state["points"] if p["label"] == "M-BAD"][0]
check("MISS 测点标记为无效", not bad["valid"] and any(i.startswith("missing-freq") for i in bad["issues"]),
      bad["issues"])

R = state["limits"]["influence_radius"]
near = [c for c in state["cells"] if (c["x"] - 10) ** 2 + (c["y"] - 10) ** 2 <= R ** 2]
check("MISS 影响半径内网格数量>0", len(near) > 0, "%d 格" % len(near))
ok_near = [c for c in near if c["status"] == "ok"]
check("MISS 半径内无 ok 格(原反例为 15 格 ok)", len(ok_near) == 0,
      "仍有 %d 格 ok" % len(ok_near))
nc_near = [c for c in near if c["status"] == "noconclusion"]
check("MISS 半径内全部为 noconclusion", len(nc_near) == len(near),
      "%d/%d" % (len(nc_near), len(near)))
check("noconclusion 原因指明含无效测点",
      all("含无效测点" in c["reason"] and "M-BAD" in c["reason"] for c in nc_near),
      nc_near[0]["reason"] if nc_near else "-")
# 反例核心:半径内有效测点 >=3 的格子(修复前会输出 ok)也必须 noconclusion
tainted_enough = [c for c in near if c["n"] >= 3]
check("存在有效测点 >=3 的被污染格(反例场景成立)", len(tainted_enough) > 0,
      "%d 格" % len(tainted_enough))
check("有效测点 >=3 的被污染格也全部 noconclusion",
      all(c["status"] == "noconclusion" for c in tainted_enough),
      "n=%s" % sorted({c["n"] for c in tainted_enough}))
far = [c for c in state["cells"] if c["cx"] == 8 and c["cy"] == 8]  # 中心 (17,17)
check("远离无效点的有效簇仍为 ok", len(far) == 1 and far[0]["status"] == "ok",
      far[0]["status"] if far else "missing")

# ---------------------------------------------------------------- 缺陷 2:版本隔离
C.post("/api/versions/%d/decide" % V1, json={"point_label": "A", "action": "lock"})
j1 = recalc(V1)
kinds1 = [d["kind"] for d in j1["manual_decisions"]]
check("v1 导出含本版 import+lock", kinds1 == ["import", "lock"], kinds1)

rows2 = make_rows("I", 4, 16)  # v2:新增一个有效测点
s2 = import_csv(PID, "v2-巡测", csv_text(rows2))
assert s2.status_code == 200, s2.data
V2 = s2.get_json()["version"]["id"]
C.post("/api/versions/%d/decide" % V2,
       json={"point_label": "B", "action": "exclude", "reason": "调光设备测试"})

j1b = recalc(V1)
kinds1b = [d["kind"] for d in j1b["manual_decisions"]]
check("v2 产生决定后,v1 导出仍只有本版记录", kinds1b == ["import", "lock"], kinds1b)
check("v1 导出不含 v2 的 exclude",
      all(not (d["kind"] == "exclude") for d in j1b["manual_decisions"]))

j2 = recalc(V2)
kinds2 = [d["kind"] for d in j2["manual_decisions"]]
check("v2 导出只有本版 import+exclude", kinds2 == ["import", "exclude"], kinds2)
check("v2 导出不带入 v1 的 lock",
      all(d["kind"] != "lock" for d in j2["manual_decisions"]))

# ---------------------------------------------------------------- 主流程 sanity
st = C.get("/api/projects/%d/state" % PID).get_json()
check("版本列表含两个版本", len(st["versions"]) == 2)
check("前端决定日志仍跨版聚合(主流程不变)", len(st["decisions"]) >= 4,
      "%d 条" % len(st["decisions"]))
check("补测路径接口可用", C.get("/api/versions/%d/path" % V2).status_code == 200)
check("覆盖 SVG 导出可用", C.get("/api/versions/%d/export/coverage.svg" % V2).status_code == 200)
check("补测清单导出可用", C.get("/api/versions/%d/export/remeasure.csv" % V2).status_code == 200)

os.unlink(os.environ["LOOP_DB"])
print()
if FAILURES:
    print("FAILED:", len(FAILURES), "项")
    sys.exit(1)
print("ALL REGRESSION CHECKS PASSED")
