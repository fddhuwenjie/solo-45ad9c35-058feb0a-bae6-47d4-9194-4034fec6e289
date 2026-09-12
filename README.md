# 助听感应环巡测校审系统

剧场助听感应环(Hearing Loop / IEC 60118-4 场景)的巡测数据校审网页。
空场验收合格不代表满场合格——本系统把带坐标的巡测数据插值到座位图上,
按可配置限值自动校核,标出证据不足与不得下结论的区域,并规划最短补测路径。

## 运行

```bash
pip install flask          # 唯一依赖,Python 3.8+
python3 app.py             # http://127.0.0.1:5000
python3 make_sample.py     # 可选:生成 sample/ 演示数据(场地 SVG + 两期 CSV)
```

数据库自动建于 `data/loop_survey.db`(可用环境变量 `LOOP_DB` 改路径)。

## 使用流程

1. **新建项目**:上传剧场座位图 SVG(viewBox 单位视为米)。
2. **导入巡测 CSV**:每次导入生成一个新版本;新增/复测数据只触发相关网格增量重算。
3. **圈定分区**:观众区(只在其内下结论)、禁测区、干扰设备(单击放置,结果标"临近干扰")。
4. **校审**:座位图上叠加覆盖网格;点击测点看频响曲线(与座位图联动);
   拖动误定位点修正坐标;为临时异常(如调光设备测试)填写排除理由;复核完成后锁定测点。
5. **补测**:一键规划覆盖全部问题区域的最短路径(区域聚类 + 最近邻/2-opt)。
6. **导出**:覆盖 SVG、补测清单 CSV、复算 JSON(含限值、校准摘要、人工决定、逐格结论)。

## CSV 格式

```csv
point_id,x,y,freq_hz,field_db,noise_db,device_id,calib_version
P01,8.0,10.0,1000,-6.2,-38.1,FSM-01,CAL-2026A
```

每行 = 某测点某频点的一条测量;`field_db` 为场强(dB),`noise_db` 为背景噪声。
同一 `point_id` 多行组成该点的频响。再次导入同名测点 = 复测替换(已锁定测点拒绝覆盖)。

## 校核规则(全部可在左栏配置)

| 检查项 | 默认限值 |
|---|---|
| 场强范围 | −12 ~ 0 dB(参考频点 1000 Hz) |
| 座位间均匀度 | ≤ 6 dB(影响半径内测点 max−min) |
| 信噪比 | ≥ 20 dB |
| 频响偏差 | ≤ ±3 dB(100 Hz–5 kHz 相对参考频点) |

网格状态:`合格 / 临近干扰 / 不合格 / 证据不足(虚线框) / 不作结论 / 禁测区`。
覆盖值用 IDW(反距离加权)插值;影响半径内有效测点少于阈值即"证据不足"。

**不作结论**(紫色)的触发条件:坐标越界、频点缺失、重复测点(同名冲突或空间过近)、
以及网格贡献测点中混用不同设备校准版本。无效测点不参与任何区域结论。

## 版本与审计

- 每次导入生成新版本,可随时切换查看历史快照;
- 每版保留校准摘要(设备/校准版本/混用标记)与全部人工决定
  (导入、移动、锁定、排除及理由),并汇入复算 JSON;
- 锁定 = 已复核:不可拖动、不可排除、不可被新 CSV 覆盖。

## 主要 API

```
POST /api/projects                    建项目(场地 SVG)
POST /api/projects/<id>/import        导入 CSV,生成新版本(增量重算)
PUT  /api/projects/<id>/limits        改限值(全量重算)
POST /api/projects/<id>/zones         加分区(全量重算)
POST /api/versions/<id>/move          移动测点(增量重算)
POST /api/versions/<id>/decide        lock/unlock/exclude/include
GET  /api/versions/<id>/path          最短补测路径
GET  /api/versions/<id>/export/coverage.svg | remeasure.csv | recalc.json
```
