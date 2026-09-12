"""生成演示数据:sample/venue.svg、sample/measurements_v1.csv、sample/measurements_v2.csv

场景:60m x 40m 剧场,舞台在北侧。v1 为空场验收数据,故意包含:
  - 一个坐标越界点、一对空间重复点、一个缺频点的点
  - 两种设备校准版本混用(CAL-2025B / CAL-2026A)
  - 左前区场强衰减(楼座下遮挡)、右侧调光设备附近背景噪声抬升
v2 为补测数据:统一校准版本,填补左前弱区并复测调光设备附近点。
"""
import csv
import math
import os
import random

random.seed(42)
OUT = os.path.join(os.path.dirname(__file__), "sample")
FREQS = [100, 500, 1000, 2000, 4000, 5000]
W, H = 60.0, 40.0
DIMMER = (56.0, 20.0)   # 调光设备位置(右墙中部)


def field_model(x, y):
    """环路线圈覆盖中部,左前楼座下衰减,边缘滚降。"""
    base = -6.0
    edge = min(x, W - x, y, H - y)
    base += min(edge, 8.0) * 0.25 - 2.0          # 边缘滚降
    if x < 18 and y > 26:                        # 左前弱区
        base -= 9.0 * (1 - x / 18.0) * ((y - 26) / 14.0)
    ripple = 1.2 * math.sin(x / 5.0) * math.cos(y / 6.0)
    return base + ripple + random.uniform(-0.4, 0.4)


def noise_model(x, y):
    d = math.hypot(x - DIMMER[0], y - DIMMER[1])
    n = -38.0 + random.uniform(-1.0, 1.0)
    if d < 10:                                    # 调光设备交流声
        n += 14.0 * (1 - d / 10.0)
    return n


def freq_shape(x, y, f):
    """频响:低频略抬、高频在弱区下跌。"""
    s = 0.0
    if f <= 100:
        s += 1.5
    if f >= 4000 and x < 18 and y > 26:
        s -= 5.0
    if f >= 4000:
        s -= 1.0
    return s


def venue_svg():
    seats = []
    for r in range(12):                            # 12 排座位
        y = 10 + r * 2.4
        for c in range(20):
            x = 8 + c * 2.3
            if 27 < x < 33 and r < 3:              # 中部过道
                continue
            seats.append("<rect x='%.1f' y='%.1f' width='1.6' height='1.4' rx='0.3' "
                         "fill='#3d4656'/>" % (x, y))
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 60 40">
<rect x='0' y='0' width='60' height='40' fill='#20242c' stroke='#7a8494' stroke-width='0.4'/>
<rect x='10' y='1' width='40' height='6' fill='#4a3b2a' stroke='#8a7a5a' stroke-width='0.3'/>
<text x='30' y='4.6' font-size='2.6' fill='#c8b088' text-anchor='middle'>舞台 STAGE</text>
<rect x='52' y='16' width='7' height='8' fill='#4a2a2a' stroke='#a06a6a' stroke-width='0.3'/>
<text x='55.5' y='20.6' font-size='1.8' fill='#d8a0a0' text-anchor='middle'>调光室</text>
<rect x='2' y='28' width='14' height='10' fill='none' stroke='#5a6474' stroke-width='0.25' stroke-dasharray='1 1'/>
<text x='9' y='33.6' font-size='1.8' fill='#7a8494' text-anchor='middle'>楼座下</text>
%s
</svg>""" % "\n".join(seats)


def point_grid():
    pts = []
    idx = 1
    for y in range(10, 37, 4):
        for x in range(8, 55, 5):
            pts.append(("P%02d" % idx, float(x), float(y)))
            idx += 1
    return pts


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["point_id", "x", "y", "freq_hz", "field_db", "noise_db",
                    "device_id", "calib_version"])
        w.writerows(rows)


def main():
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "venue.svg"), "w", encoding="utf-8") as f:
        f.write(venue_svg())

    rows = []
    pts = point_grid()
    for i, (label, x, y) in enumerate(pts):
        # 一半测点用旧校准版本 -> 触发"校准版本混用,不作结论"
        calib = "CAL-2025B" if i % 2 == 0 else "CAL-2026A"
        dev = "FSM-01" if i % 2 == 0 else "FSM-02"
        for f in FREQS:
            field = field_model(x, y) + freq_shape(x, y, f)
            noise = noise_model(x, y)
            rows.append([label, x, y, f, round(field, 1), round(noise, 1), dev, calib])

    # 异常 1:坐标越界
    for f in FREQS:
        rows.append(["P-OOB", -3.0, 20.0, f, -7.0, -38.0, "FSM-01", "CAL-2025B"])
    # 异常 2:空间重复(与 P01 相距 0.2m)
    for f in FREQS:
        rows.append(["P-DUP", 8.2, 10.1, f, -6.5, -38.0, "FSM-01", "CAL-2025B"])
    # 异常 3:频点缺失(少 4000/5000)
    for f in [100, 500, 1000, 2000]:
        rows.append(["P-MISS", 30.0, 22.0, f, -6.8, -37.5, "FSM-02", "CAL-2026A"])
    write_csv(os.path.join(OUT, "measurements_v1.csv"), rows)

    # v2:发现校准混用后,用统一设备复测全部 CAL-2025B 测点,并补左前弱区与调光设备附近
    rows2 = []
    for i, (label, x, y) in enumerate(pts):
        if i % 2 == 0:  # 原 CAL-2025B 测点复测,统一为 CAL-2026A
            for f in FREQS:
                field = field_model(x, y) + freq_shape(x, y, f)
                noise = noise_model(x, y)
                rows2.append([label, x, y, f, round(field, 1), round(noise, 1),
                              "FSM-01", "CAL-2026A"])
    extra = [("R01", 6, 30), ("R02", 10, 33), ("R03", 14, 29), ("R04", 8, 35),
             ("R05", 50, 18), ("R06", 52, 24), ("R07", 46, 22)]
    for label, x, y in extra:
        for f in FREQS:
            field = field_model(x, y) + freq_shape(x, y, f)
            noise = noise_model(x, y)
            rows2.append([label, x, y, f, round(field, 1), round(noise, 1),
                          "FSM-01", "CAL-2026A"])
    # 复测 P-MISS,补齐频点(替换旧数据)
    for f in FREQS:
        rows2.append(["P-MISS", 30.0, 22.0, f, -6.8, -37.5, "FSM-02", "CAL-2026A"])
    write_csv(os.path.join(OUT, "measurements_v2.csv"), rows2)
    print("written to", OUT)


if __name__ == "__main__":
    main()
