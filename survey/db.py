"""SQLite 持久化:项目、巡测版本、测量行、分区、限值、网格缓存、人工决定、复测对照。"""
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
