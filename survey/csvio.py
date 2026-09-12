"""巡测 CSV 解析。列:point_id,x,y,freq_hz,field_db,noise_db[,device_id,calib_version]"""
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
