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
            errors.append("第 %d 行: 测点 %s 重复(一次测次中每点仅允许一条读数)" % (ln, label))
        seen.add(label)
        rows.append(r)
    return rows, errors
