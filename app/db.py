"""SQLite (hilos seguros) + ajustes."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

_lock = threading.RLock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT DEFAULT '', source TEXT DEFAULT '',
  status TEXT DEFAULT 'queued', progress REAL DEFAULT 0, message TEXT DEFAULT '', options TEXT DEFAULT '{}',
  language TEXT DEFAULT '', duration REAL DEFAULT 0, created_at INTEGER, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, title TEXT, start REAL, end REAL,
  score INTEGER DEFAULT 0, reason TEXT DEFAULT '', path TEXT, thumb TEXT, words TEXT DEFAULT '[]',
  transcript TEXT DEFAULT '', theme TEXT DEFAULT '', layout TEXT DEFAULT '', source_path TEXT DEFAULT '',
  created_at INTEGER);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, ts INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS editor_projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT DEFAULT '', spec TEXT DEFAULT '{}', status TEXT DEFAULT 'idle',
  progress REAL DEFAULT 0, message TEXT DEFAULT '', export_clip_id INTEGER DEFAULT 0, created_at INTEGER,
  updated_at INTEGER);
CREATE TABLE IF NOT EXISTS editor_assets(
  id TEXT PRIMARY KEY, name TEXT, kind TEXT, path TEXT, duration REAL DEFAULT 0, created_at INTEGER);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, name TEXT DEFAULT '',
  pass_hash TEXT DEFAULT '', salt TEXT DEFAULT '', is_admin INTEGER DEFAULT 0,
  approved INTEGER DEFAULT 0, created_at INTEGER);
CREATE TABLE IF NOT EXISTS social_accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER DEFAULT 0, platform TEXT,
  account_name TEXT DEFAULT '', account_ref TEXT DEFAULT '',
  access_token TEXT DEFAULT '', refresh_token TEXT DEFAULT '', expires_at INTEGER DEFAULT 0,
  scopes TEXT DEFAULT '', connected_at INTEGER,
  UNIQUE(user_id, platform, account_ref));
CREATE TABLE IF NOT EXISTS social_posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER DEFAULT 0, clip_id INTEGER DEFAULT 0,
  platform TEXT, account_id INTEGER DEFAULT 0, title TEXT DEFAULT '', hashtags TEXT DEFAULT '',
  publish_at INTEGER DEFAULT 0, status TEXT DEFAULT 'queued',
  external_id TEXT DEFAULT '', external_url TEXT DEFAULT '', error TEXT DEFAULT '',
  created_at INTEGER, updated_at INTEGER);
"""


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(config.DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


# columnas añadidas después de la v2.0 (se agregan a bases de datos existentes sin perder nada)
MIGRATIONS = [
    ("jobs", "creator", "TEXT DEFAULT ''"), ("jobs", "creator_key", "TEXT DEFAULT ''"),
    ("jobs", "platform", "TEXT DEFAULT ''"), ("jobs", "user_id", "INTEGER DEFAULT 0"),
    ("clips", "creator", "TEXT DEFAULT ''"), ("clips", "creator_key", "TEXT DEFAULT ''"),
    ("clips", "origin", "TEXT DEFAULT ''"), ("clips", "editor_id", "INTEGER DEFAULT 0"),
    ("clips", "moment", "TEXT DEFAULT ''"), ("clips", "summary", "TEXT DEFAULT ''"),
    ("editor_projects", "user_id", "INTEGER DEFAULT 0"),
]


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).lastrowid


def now() -> int:
    return int(time.time())


def kv_get(key, default=None):
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def kv_set(key, value):
    run("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)))


def get_settings() -> dict:
    s = dict(config.DEFAULT_SETTINGS)
    s.update({k: v for k, v in (kv_get("settings", {}) or {}).items() if k in s})
    return s


def set_settings(patch: dict):
    cur = kv_get("settings", {}) or {}
    for k, v in patch.items():
        if k in config.DEFAULT_SETTINGS:
            default = config.DEFAULT_SETTINGS[k]
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                v = int(v)
            elif isinstance(default, float):
                v = float(v)
            else:
                v = str(v)
            cur[k] = v
    kv_set("settings", cur)


# ---- jobs / eventos ----
def job_update(job_id: int, **f):
    if not f:
        return
    f["updated_at"] = now()
    sets = ",".join(f"{k}=?" for k in f)
    run(f"UPDATE jobs SET {sets} WHERE id=?", (*f.values(), job_id))


def log(job_id: int, text: str):
    run("INSERT INTO events(job_id,ts,text) VALUES(?,?,?)", (job_id, now(), text[:500]))
