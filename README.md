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
以及网格贡献测点中混用不同设备校准版本。无效测点不参与任何区域结论;
且其**影响半径内的网格一律不作结论**——即使附近另有足够有效测点,
也不输出插值结果(复测补齐数据后污染自动解除)。

复算 JSON 中的 `manual_decisions` 按版本隔离:每个版本只导出本版产生的
人工决定,不带入后续或历史版本的记录(前端"人工决定记录"面板仍跨版聚合展示)。

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

## 边界外逸工作区(`/leakage`)

相邻排练厅感应环同时启用时,本环磁场越过保密边界串入相邻接收器的专项校审:
先用**关闭工况**沿线估计背景,再与**开启工况**测点配对、按路径里程插值外逸量
(开启 − 关闭背景,dB),给出连续超限长度、峰值位置和相邻环余量。

测次 CSV 列:`point_id,x,y,field_db[,background_db][,calib_version][,device_id][,time]`。

**相关边界段保持无结论**的五种情形:两测次时间相隔超过时间窗(时段不重叠)、
测点配对多解、保密边界路径自交、沿线采样间距超过限值、配对校准依据不兼容。
沿线取点同时按**垂距**与**沿路径里程差**卡影响半径——U 形边界另一支路
空间近邻(如 6 m)但沿线远隔(如 38 m)的测点不会混入本支路 2 m 测站。

人工改配、调整边界顶点/拆分、拖动误定位测点均**必须备注**,且每次成功变更
生成并切换至**新修订**(事件挂对应修订号);确认后锁定来源测次、配对表、
限值与边界,三份导出(标色边界 SVG / 复测点 CSV / 复算 JSON)取自同一确认结果,
重审后修订号 +1。

```
POST /api/projects/<pid>/leak-surveys              建校审
POST /api/leak-surveys/<id>/runs                   导入开/关工况测次(CSV)
POST /api/leak-surveys/<id>/zones | /paths         圈环区 / 画保密边界
POST /api/leak-surveys/<id>/overrides              人工改配(备注,新修订)
PUT  /api/leak-paths/<id>/vertices                 调顶点/拆分(备注,新修订)
POST /api/leak-surveys/<id>/move                   拖动误定位点(备注,新修订)
POST /api/leak-surveys/<id>/confirm | /reopen      确认锁定 / 重审(修订+1)
GET  /api/leak-surveys/<id>/export/boundary.svg | remeasure.csv | recalc.json
```

回归:`python3 regression_leak.py`。

## 驱动基准工作区(`/drive`)

一次剧场巡测常要走上几十分钟,功放可能限幅、过热或输入电平改变而输出漂移;
若把各时刻读数当作同一驱动条件,弱场区可能只是当时环路电流下降。本工作区
用**带时标的环路电流记录**把场强读数归一化到同一驱动基准:

1. **功放记录**:上传 `time,current_a[,clip][,overheat]` CSV(时标支持秒数、
   HH:MM[:SS]、ISO 日期时间);
2. **场强记录**:上传 `point_id,x,y,freq_hz,field_db,noise_db,time[,device_id][,calib_version]`;
3. **时钟锚点**:为功放时钟与场强时钟绑定若干对应时刻(换绑必须备注理由,
   形成新修订),按锚点生成分段时码映射;座位图与电流时间曲线联动核对绑定;
4. **归一化**:测点按映射归入电流区间取插值电流,以冻结的参考电流换算
   `校正值 dB = 20·log10(I参考/I实际)`;原始读数始终并列展示。

以下情形相应测点**不得进入覆盖计算**(座位图紫点,原因明示):
映射倒退、采样断档、削波、过热、校准量程不足、归一化幅度越限、超出锚点范围。
削波/过热/断档可用**保留异常段**豁免(必须备注理由,形成新修订):
断档保留后若两侧有样本,按跨档插值换算,测点有 `current_a`/`norm_ref_db`
并进入对应网格;若超出记录范围无可换算电流,保留段也不能豁免,仍禁入。

**修订快照**:每次重算按当前修订号独立保存结果快照(含当时的锚点、保留段、
测点状态与电流样本),锚点/保留段变更不覆盖旧结果;历史修订可在左栏
「历史修订」面板回查(只读),并以 `/revisions/<rev>/export/...` 导出当时的
SVG/CSV/JSON。复测对照只能引用**已确认**的驱动版本(冻结结果逐点比对)。

```
POST /api/projects/<pid>/drive-surveys            建校审
POST /api/drive-surveys/<id>/record | /points     导入功放记录 / 场强记录(CSV)
POST /api/drive-surveys/<id>/anchors              绑定锚点(备注,新修订)
DELETE /api/drive-anchors/<id>                    删除锚点(备注,新修订)
POST /api/drive-surveys/<id>/keeps                保留异常段(备注,新修订)
PUT  /api/drive-surveys/<id>/params               参考电流/断档/归一化限值/校准量程
POST /api/drive-surveys/<id>/confirm | /reopen    确认锁定 / 重审(修订+1)
GET  /api/drive-surveys/<id>/revisions/<rev>      历史修订快照回查
GET  /api/drive-surveys/<id>/export/drive.svg | points.csv | recalc.json
GET  /api/drive-surveys/<id>/revisions/<rev>/export/...   历史修订导出
POST /api/projects/<pid>/drive-compares           复测对照(仅已确认版本)
```

回归:`python3 regression_drive.py`。
