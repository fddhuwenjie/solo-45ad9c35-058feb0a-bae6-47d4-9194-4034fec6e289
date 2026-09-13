"""巡测 CSV 解析。列:point_id,x,y,freq_hz,field_db,noise_db[,device_id,calib_version]
另支持边界外逸测次 CSV:point_id,x,y,field_db[,background_db][,calib_version][,time]
"""
import csv
import io

REQUIRED = ["point_id", "x", "y", "freq_hz", "field_db", "noise_db"]
OPTIONAL = ["device_id", "calib_version"]

# 常见表头别名,统一映射到标准列名
ALIASES = {
    "point": "point_id", "label": "point_id", "id": "point_id", "point_label": "point_id",
    "freq": "freq_hz", "frequency": "freq_hz", "hz": "freq_hz",
    "field": "field_db", "level": "field_db", "field_db(re1a/m)": "field_db",
    "noise": "noise_db", "background": "noise_db", "background_noise": "noise_db",
    "device": "device_id", "calib": "calib_version", "calibration": "calib_version",
}


def parse_measurements_csv(text):
    """解析 CSV 文本,返回 (rows, errors)。任一必需列缺失或数值非法即报错。"""
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV 为空或缺少表头"]
    header = {}
    for raw in reader.fieldnames:
        key = (raw or "").strip().lower()
        key = ALIASES.get(key, key)
        header[raw] = key
    missing = [c for c in REQUIRED if c not in header.values()]
    if missing:
        return [], ["缺少必需列: " + ", ".join(missing)]

    rows, errors = [], []
    for ln, raw_row in enumerate(reader, start=2):
        row = {header[k]: (v.strip() if isinstance(v, str) else v) for k, v in raw_row.items() if k is not None}
        if not any(row.values()):
            continue
        label = (row.get("point_id") or "").strip()
        if not label:
            errors.append("第 %d 行: point_id 为空" % ln)
            continue
        try:
            r = {
                "point_label": label,
                "x": float(row["x"]),
                "y": float(row["y"]),
                "freq_hz": float(row["freq_hz"]),
                "field_db": float(row["field_db"]),
                "noise_db": float(row["noise_db"]),
                "device_id": (row.get("device_id") or "").strip(),
                "calib_version": (row.get("calib_version") or "").strip(),
            }
        except (TypeError, ValueError):
            errors.append("第 %d 行: 数值列无法解析" % ln)
            continue
        if r["freq_hz"] <= 0:
            errors.append("第 %d 行: freq_hz 必须为正" % ln)
            continue
        rows.append(r)
    return rows, errors


# ---------------------------------------------------------------- 边界外逸测次

LEAK_REQUIRED = ["point_id", "x", "y", "field_db"]
LEAK_OPTIONAL = ["background_db", "calib_version", "device_id", "time", "measured_at"]

LEAK_ALIASES = {
    "point": "point_id", "label": "point_id", "id": "point_id", "point_label": "point_id",
    "field": "field_db", "level": "field_db", "field_dbu": "field_db",
    "background": "background_db", "noise": "background_db", "bg": "background_db",
    "calib": "calib_version", "calibration": "calib_version",
    "device": "device_id", "measured_at": "time", "timestamp": "time", "datetime": "time",
}


def parse_leak_csv(text):
    """解析边界外逸测次 CSV(单点单值,非频响),返回 (rows, errors)。

    必填: point_id,x,y,field_db;可选: background_db(现场背景读数)、
    calib_version、device_id、time(测量时间,用于开/关时段不重叠校验)。
    """
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV 为空或缺少表头"]
    header = {}
    for raw in reader.fieldnames:
        key = (raw or "").strip().lower()
        key = LEAK_ALIASES.get(key, key)
        header[raw] = key
    missing = [c for c in LEAK_REQUIRED if c not in header.values()]
    if missing:
        return [], ["缺少必需列: " + ", ".join(missing)]

    rows, errors = [], []
    seen = set()
    for ln, raw_row in enumerate(reader, start=2):
        row = {header[k]: (v.strip() if isinstance(v, str) else v)
               for k, v in raw_row.items() if k is not None}
        if not any(row.values()):
            continue
        label = (row.get("point_id") or "").strip()
        if not label:
            errors.append("第 %d 行: point_id 为空" % ln)
            continue
        try:
            r = {
                "point_label": label,
                "x": float(row["x"]), "y": float(row["y"]),
                "field_db": float(row["field_db"]),
                "background_db": (float(row["background_db"])
                                  if row.get("background_db") else None),
                "calib_version": (row.get("calib_version") or "").strip(),
                "device_id": (row.get("device_id") or "").strip(),
                "time": (row.get("time") or "").strip(),
            }
        except (TypeError, ValueError):
            errors.append("第 %d 行: 数值列无法解析" % ln)
            continue
        if label in seen:
            errors.append("第 %d 行: 测点 %s 重复(一次测次中每点仅允许一条读数),该行已跳过"
                          % (ln, label))
            continue
        seen.add(label)
        rows.append(r)
    return rows, errors


# ---------------------------------------------------------------- 驱动基准

def _flag(v):
    """削波/过热等告警列的宽松布尔解析。"""
    return 1 if str(v or "").strip().lower() in \
        ("1", "true", "yes", "y", "clip", "oh", "alarm", "是", "告警") else 0


DRIVE_CUR_REQUIRED = ["time", "current_a"]
DRIVE_CUR_ALIASES = {
    "t": "time", "timestamp": "time", "datetime": "time", "measured_at": "time",
    "current": "current_a", "i_a": "current_a", "amps": "current_a",
    "loop_current": "current_a", "current_a(a)": "current_a",
    "clip_alarm": "clip", "clipping": "clip", "clip_flag": "clip",
    "oh": "overheat", "thermal": "overheat", "temp_alarm": "overheat",
    "overtemp": "overheat",
}


def parse_drive_current_csv(text):
    """解析功放记录 CSV(带时标的环路电流与削波/过热告警),返回 (rows, errors)。

    必填: time,current_a;可选: clip,overheat。time 支持纯秒数、HH:MM[:SS]、
    ISO 日期时间;无法解析时刻的行跳过并记警告。
    """
    from survey.drive import parse_clock  # 避免循环导入(drive 不依赖 csvio)
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV 为空或缺少表头"]
    header = {}
    for raw in reader.fieldnames:
        key = (raw or "").strip().lower()
        key = DRIVE_CUR_ALIASES.get(key, key)
        header[raw] = key
    missing = [c for c in DRIVE_CUR_REQUIRED if c not in header.values()]
    if missing:
        return [], ["缺少必需列: " + ", ".join(missing)]

    rows, errors = [], []
    for ln, raw_row in enumerate(reader, start=2):
        row = {header[k]: (v.strip() if isinstance(v, str) else v)
               for k, v in raw_row.items() if k is not None}
        if not any(row.values()):
            continue
        t_text = (row.get("time") or "").strip()
        t_sec = parse_clock(t_text)
        if t_sec is None:
            errors.append("第 %d 行: time 无法解析(%s)" % (ln, t_text or "空"))
            continue
        try:
            cur = float(row["current_a"])
        except (TypeError, ValueError):
            errors.append("第 %d 行: current_a 无法解析" % ln)
            continue
        rows.append({"t_text": t_text, "t_sec": t_sec, "current_a": cur,
                     "clip": _flag(row.get("clip")),
                     "overheat": _flag(row.get("overheat"))})
    dup = len(rows) - len({r["t_sec"] for r in rows})
    if dup > 0:
        errors.append("相同时刻的样本 %d 条,仅各自保留一条" % dup)
        seen_t = set()
        uniq = []
        for r in rows:
            if r["t_sec"] in seen_t:
                continue
            seen_t.add(r["t_sec"])
            uniq.append(r)
        rows = uniq
    rows.sort(key=lambda r: r["t_sec"])
    return rows, errors


DRIVE_PT_REQUIRED = ["point_id", "x", "y", "freq_hz", "field_db", "noise_db", "time"]
DRIVE_PT_ALIASES = dict(ALIASES, **{
    "t": "time", "timestamp": "time", "datetime": "time", "measured_at": "time",
})


def parse_drive_points_csv(text):
    """解析带时标的场强记录 CSV,返回 (rows, errors)。

    必填: point_id,x,y,freq_hz,field_db,noise_db,time;可选: device_id,calib_version。
    同一测点不同频点可各有时刻(巡测走动中逐点逐频测量)。
    """
    from survey.drive import parse_clock
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV 为空或缺少表头"]
    header = {}
    for raw in reader.fieldnames:
        key = (raw or "").strip().lower()
        key = DRIVE_PT_ALIASES.get(key, key)
        header[raw] = key
    missing = [c for c in DRIVE_PT_REQUIRED if c not in header.values()]
    if missing:
        return [], ["缺少必需列: " + ", ".join(missing)]

    rows, errors = [], []
    for ln, raw_row in enumerate(reader, start=2):
        row = {header[k]: (v.strip() if isinstance(v, str) else v)
               for k, v in raw_row.items() if k is not None}
        if not any(row.values()):
            continue
        label = (row.get("point_id") or "").strip()
        if not label:
            errors.append("第 %d 行: point_id 为空" % ln)
            continue
        t_text = (row.get("time") or "").strip()
        t_sec = parse_clock(t_text)
        if t_sec is None:
            errors.append("第 %d 行: time 无法解析(%s)" % (ln, t_text or "空"))
            continue
        try:
            r = {
                "point_label": label,
                "x": float(row["x"]), "y": float(row["y"]),
                "freq_hz": float(row["freq_hz"]),
                "field_db": float(row["field_db"]),
                "noise_db": float(row["noise_db"]),
                "t_text": t_text, "t_sec": t_sec,
                "device_id": (row.get("device_id") or "").strip(),
                "calib_version": (row.get("calib_version") or "").strip(),
            }
        except (TypeError, ValueError):
            errors.append("第 %d 行: 数值列无法解析" % ln)
            continue
        if r["freq_hz"] <= 0:
            errors.append("第 %d 行: freq_hz 必须为正" % ln)
            continue
        rows.append(r)
    return rows, errors
