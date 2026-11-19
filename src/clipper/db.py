"""SQLite persistence. One connection per thread, WAL mode, simple helper API."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (          -- rights allowlist (default deny)
  platform TEXT NOT NULL, channel_id TEXT NOT NULL, name TEXT,
  status TEXT NOT NULL CHECK (status IN ('allowed','denied')),
  basis TEXT, note TEXT, updated_at REAL NOT NULL,
  PRIMARY KEY (platform, channel_id));
CREATE TABLE IF NOT EXISTS streams (
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, video_id TEXT NOT NULL,
  channel_id TEXT, channel_name TEXT, title TEXT, url TEXT, viewers INTEGER DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'discovered',    -- discovered|blocked|recording|ended|error
  state_reason TEXT, discovered_at REAL, started_at REAL, ended_at REAL,
  meta TEXT, UNIQUE (platform, video_id));
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY, stream_id INTEGER NOT NULL REFERENCES streams(id),
  path TEXT NOT NULL UNIQUE, start_ts REAL NOT NULL, duration REAL,
  status TEXT NOT NULL DEFAULT 'open',          -- open|complete|bad|analyzed|deleted
  protected INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS seg_stream ON segments(stream_id, start_ts);
CREATE TABLE IF NOT EXISTS audio_sec (          -- per-second energy, independent of segment boundaries
  stream_id INTEGER NOT NULL, sec INTEGER NOT NULL, sumsq REAL NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY (stream_id, sec));
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY, stream_id INTEGER NOT NULL, ts REAL NOT NULL,
  author TEXT, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chat_stream_ts ON chat_messages(stream_id, ts);
CREATE TABLE IF NOT EXISTS windows (
  stream_id INTEGER NOT NULL, t_start REAL NOT NULL, t_end REAL NOT NULL,
  chat_count INTEGER NOT NULL, chat_authors INTEGER NOT NULL DEFAULT 0, loudness_db REAL,
  chat_z REAL, loud_z REAL, PRIMARY KEY (stream_id, t_start));
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY, stream_id INTEGER NOT NULL, event_ts REAL NOT NULL,
  chat_z REAL, loud_z REAL, score REAL, reasons TEXT,
  status TEXT NOT NULL DEFAULT 'pending',       -- pending|ready|processing|done|rejected|failed|superseded
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL,
  UNIQUE (stream_id, event_ts));
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY, candidate_id INTEGER, stream_id INTEGER,
  path TEXT, title TEXT, status TEXT NOT NULL DEFAULT 'draft',  -- draft|review|rejected_qc|approved|rejected|needs_edit
  quality TEXT, meta_path TEXT, created_at REAL NOT NULL, reviewed_at REAL, review_note TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT, component TEXT, message TEXT, stream_id INTEGER);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._mem_conn: sqlite3.Connection | None = None
        self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        if self.path == ":memory:":  # single shared connection for tests
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
            return self._mem_conn
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def insert_many(self, sql: str, rows: list[tuple]) -> None:
        if rows:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def log_event(self, level: str, component: str, message: str,
                  stream_id: int | None = None, ts: float | None = None) -> None:
        import time
        self.execute("INSERT INTO events(ts,level,component,message,stream_id) VALUES (?,?,?,?,?)",
                     (ts or time.time(), level, component, message[:2000], stream_id))

    @staticmethod
    def dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, default=str)

    @staticmethod
    def loads(s: str | None, default: Any = None) -> Any:
        try:
            return json.loads(s) if s else default
        except (TypeError, json.JSONDecodeError):
            return default  # malformed metadata must never crash the pipeline
