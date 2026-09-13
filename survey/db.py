"""SQLite 持久化:项目、巡测版本、测量行、分区、限值、网格缓存、人工决定、
复测对照,边界外逸工作区(测次/测点/环区/保密边界/改配/修订事件),
以及驱动基准工作区(功放记录/电流样本/场强记录/时钟锚点/保留段/修订事件/对照)。"""
import os
import sqlite3

DB_PATH = os.environ.get(
    "LOOP_DB",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "loop_survey.db")),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  venue_svg TEXT NOT NULL,
  bounds_json TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS versions(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  label TEXT NOT NULL,
  source TEXT DEFAULT 'csv-import',
  calibration_summary_json TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS measurements(
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES versions(id),
  point_label TEXT NOT NULL,
  x REAL NOT NULL, y REAL NOT NULL,
  freq_hz REAL NOT NULL,
  field_db REAL NOT NULL,
  noise_db REAL NOT NULL,
  device_id TEXT DEFAULT '',
  calib_version TEXT DEFAULT '',
  locked INTEGER DEFAULT 0,
  excluded INTEGER DEFAULT 0,
  exclude_reason TEXT DEFAULT '',
  moved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_meas_version ON measurements(version_id, point_label);
CREATE TABLE IF NOT EXISTS zones(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL,
  name TEXT DEFAULT '',
  polygon_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS limits(
  project_id INTEGER PRIMARY KEY REFERENCES projects(id),
  field_min REAL DEFAULT -12.0,
  field_max REAL DEFAULT 0.0,
  uniformity_db REAL DEFAULT 6.0,
  snr_min REAL DEFAULT 20.0,
  freq_dev_db REAL DEFAULT 3.0,
  ref_freq REAL DEFAULT 1000.0,
  expected_freqs TEXT DEFAULT '[100,500,1000,2000,4000,5000]',
  cell_size REAL DEFAULT 2.0,
  influence_radius REAL DEFAULT 6.0,
  min_points INTEGER DEFAULT 3
);
CREATE TABLE IF NOT EXISTS grid_cells(
  version_id INTEGER NOT NULL REFERENCES versions(id),
  cx INTEGER NOT NULL, cy INTEGER NOT NULL,
  x REAL NOT NULL, y REAL NOT NULL,
  field_db REAL, snr_db REAL, uniformity_db REAL, freq_dev_db REAL,
  status TEXT NOT NULL, reason TEXT DEFAULT '',
  n_points INTEGER DEFAULT 0,
  PRIMARY KEY(version_id, cx, cy)
);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES versions(id),
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS comparisons(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  base_version_id INTEGER NOT NULL REFERENCES versions(id),
  retest_version_id INTEGER NOT NULL REFERENCES versions(id),
  label TEXT DEFAULT '',
  base_occ TEXT DEFAULT '', base_lighting TEXT DEFAULT '', base_pa TEXT DEFAULT '',
  retest_occ TEXT DEFAULT '', retest_lighting TEXT DEFAULT '', retest_pa TEXT DEFAULT '',
  pos_tol REAL DEFAULT 1.0,
  status TEXT DEFAULT 'draft',
  result_json TEXT,
  source_max_version_id INTEGER,
  created_at TEXT DEFAULT (datetime('now')),
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS pair_overrides(
  id INTEGER PRIMARY KEY,
  comparison_id INTEGER NOT NULL REFERENCES comparisons(id),
  base_label TEXT NOT NULL,
  retest_label TEXT,
  note TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS comparison_events(
  id INTEGER PRIMARY KEY,
  comparison_id INTEGER NOT NULL REFERENCES comparisons(id),
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

-- ---------------- 边界外逸工作区 ----------------
-- 一次边界外逸校审:选定本环 + 相邻环,导入开/关两种工况测次
CREATE TABLE IF NOT EXISTS leak_surveys(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  label TEXT DEFAULT '',
  own_loop TEXT DEFAULT '',          -- 本环名称
  adjacent_loop TEXT DEFAULT '',     -- 相邻环名称
  leak_limit_db REAL DEFAULT 6.0,    -- 外逸量限值 dB(开-关背景)
  max_field_db REAL DEFAULT -32.0,   -- 边界外绝对场强限值 dB
  min_points INTEGER DEFAULT 2,      -- 插值所需最少有效配对测点
  influence_radius REAL DEFAULT 8.0, -- 测站影响半径 m
  max_sample_gap REAL DEFAULT 4.0,   -- 沿线允许最大采样间距 m
  max_time_gap_h REAL DEFAULT 2.0,   -- 开/关测次时间窗(小时),超过则时段不重叠
  status TEXT DEFAULT 'draft',       -- draft | confirmed
  result_json TEXT,
  revision INTEGER DEFAULT 1,        -- 人工改配/调边界使修订号 +1
  source_on_run_id INTEGER,          -- 确认时锁定的来源测次
  source_off_run_id INTEGER,
  created_at TEXT DEFAULT (datetime('now')),
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS leak_runs(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES leak_surveys(id),
  condition TEXT NOT NULL,           -- on(环路开启) | off(关闭,估背景)
  label TEXT DEFAULT '',
  time_text TEXT DEFAULT '',         -- 测量时间(文本,解析失败不阻断但不做时段校验)
  time_iso TEXT,                     -- 解析后的 ISO 时间(可空)
  device_id TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS leak_points(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES leak_runs(id),
  point_label TEXT NOT NULL,
  x REAL NOT NULL, y REAL NOT NULL,
  field_db REAL NOT NULL,
  background_db REAL,                -- 现场记录的背景读数(可空),仅参考
  calib_version TEXT DEFAULT '',
  device_id TEXT DEFAULT '',
  moved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_leakpts_run ON leak_points(run_id, point_label);
CREATE TABLE IF NOT EXISTS leak_zones(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES leak_surveys(id),
  kind TEXT NOT NULL,                -- own(本环服务区) | adjacent(相邻环区)
  name TEXT DEFAULT '',
  polygon_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leak_paths(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES leak_surveys(id),
  name TEXT DEFAULT '',
  vertices_json TEXT NOT NULL,       -- [[x,y],...] 折线(保密边界)
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS leak_pair_overrides(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES leak_surveys(id),
  on_label TEXT NOT NULL,
  off_label TEXT,                    -- NULL = 取消配对
  note TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS leak_events(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES leak_surveys(id),
  revision INTEGER DEFAULT 1,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

-- ---------------- 驱动基准工作区 ----------------
-- 一次驱动基准校审:功放记录(电流+告警) + 带时标场强记录 + 时钟锚点 -> 归一化覆盖
CREATE TABLE IF NOT EXISTS drive_surveys(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  label TEXT DEFAULT '',
  ref_current_a REAL DEFAULT 2.0,     -- 冻结的参考电流 A(归一化基准)
  max_sample_gap_s REAL DEFAULT 60.0, -- 电流采样最大断档 s
  max_norm_db REAL DEFAULT 3.0,       -- 归一化幅度限值 dB(|校正值|超过即越限)
  calib_min_a REAL DEFAULT 0.2,       -- 电流校准量程下限 A
  calib_max_a REAL DEFAULT 10.0,      -- 电流校准量程上限 A
  status TEXT DEFAULT 'draft',        -- draft | confirmed
  result_json TEXT,
  revision INTEGER DEFAULT 1,         -- 换绑锚点/保留异常段使修订号 +1
  source_record_id INTEGER,           -- 确认时锁定的功放记录
  created_at TEXT DEFAULT (datetime('now')),
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS drive_records(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES drive_surveys(id),
  label TEXT DEFAULT '',
  device_id TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS drive_samples(
  id INTEGER PRIMARY KEY,
  record_id INTEGER NOT NULL REFERENCES drive_records(id),
  t_text TEXT DEFAULT '',
  t_sec REAL NOT NULL,
  current_a REAL NOT NULL,
  clip INTEGER DEFAULT 0,
  overheat INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dsamp_record ON drive_samples(record_id, t_sec);
CREATE TABLE IF NOT EXISTS drive_points(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES drive_surveys(id),
  point_label TEXT NOT NULL,
  x REAL NOT NULL, y REAL NOT NULL,
  freq_hz REAL NOT NULL,
  field_db REAL NOT NULL,
  noise_db REAL NOT NULL,
  t_text TEXT DEFAULT '',
  t_sec REAL NOT NULL,
  device_id TEXT DEFAULT '',
  calib_version TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dpts_survey ON drive_points(survey_id, point_label);
CREATE TABLE IF NOT EXISTS drive_anchors(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES drive_surveys(id),
  amp_t REAL NOT NULL,                -- 功放时钟(秒)
  field_t REAL NOT NULL,              -- 场强记录时钟(秒)
  amp_text TEXT DEFAULT '',
  field_text TEXT DEFAULT '',
  note TEXT DEFAULT '',               -- 绑定/换绑理由
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS drive_keeps(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES drive_surveys(id),
  kind TEXT NOT NULL,                 -- clip | overheat | sample-gap
  t0 REAL NOT NULL, t1 REAL NOT NULL, -- 功放时钟区段(秒)
  note TEXT NOT NULL,                 -- 保留理由(必填)
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS drive_events(
  id INTEGER PRIMARY KEY,
  survey_id INTEGER NOT NULL REFERENCES drive_surveys(id),
  revision INTEGER DEFAULT 1,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS drive_compares(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  base_survey_id INTEGER NOT NULL REFERENCES drive_surveys(id),
  retest_survey_id INTEGER NOT NULL REFERENCES drive_surveys(id),
  label TEXT DEFAULT '',
  result_json TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
"""


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None
