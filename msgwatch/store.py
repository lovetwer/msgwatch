"""SQLite 状态存储：已推送去重、监控项状态、昵称缓存。"""
from __future__ import annotations

import json
import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    first_seen_ts REAL NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS monitor_state (
    monitor_id TEXT PRIMARY KEY,
    seeded INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_success_ts REAL,
    last_fail_ts REAL,
    last_alert_ts REAL
);
CREATE TABLE IF NOT EXISTS users (
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    name TEXT,
    updated REAL,
    PRIMARY KEY (platform, uid)
);
"""


class Store:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- 去重 ----------

    def has_seen(self, key: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,))
        return cur.fetchone() is not None

    def mark_seen(self, keys: list[str], notified: bool = True) -> None:
        now = time.time()
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen (key, first_seen_ts, notified) VALUES (?, ?, ?)",
            [(k, now, int(notified)) for k in keys],
        )
        self._conn.commit()

    def filter_new(self, monitor_id: str, items: list, seed_on_first_run: bool = True) -> list:
        """返回其中真正是新消息的部分；首次运行先把现有消息记为已见（seed）。"""
        cur = self._conn.execute("SELECT seeded FROM monitor_state WHERE monitor_id = ?", (monitor_id,))
        row = cur.fetchone()
        seeded = bool(row and row[0])
        if not seeded and seed_on_first_run:
            self.mark_seen([i.dedup_key for i in items], notified=False)
            self.set_state(monitor_id, seeded=True)
            return []
        return [i for i in items if not self.has_seen(i.dedup_key)]

    # ---------- 监控项状态 ----------

    def get_state(self, monitor_id: str) -> dict:
        cur = self._conn.execute(
            "SELECT seeded, fail_count, last_success_ts, last_fail_ts, last_alert_ts "
            "FROM monitor_state WHERE monitor_id = ?",
            (monitor_id,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        keys = ("seeded", "fail_count", "last_success_ts", "last_fail_ts", "last_alert_ts")
        return dict(zip(keys, row))

    def set_state(self, monitor_id: str, **fields) -> None:
        cur = self._conn.execute("SELECT monitor_id FROM monitor_state WHERE monitor_id = ?", (monitor_id,))
        if not cur.fetchone():
            self._conn.execute("INSERT INTO monitor_state (monitor_id) VALUES (?)", (monitor_id,))
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self._conn.execute(f"UPDATE monitor_state SET {cols} WHERE monitor_id = ?", (*fields.values(), monitor_id))
        self._conn.commit()

    # ---------- 昵称缓存 ----------

    def get_user(self, platform: str, uid) -> str | None:
        cur = self._conn.execute("SELECT name FROM users WHERE platform = ? AND uid = ?", (platform, str(uid)))
        row = cur.fetchone()
        return row[0] if row else None

    def put_user(self, platform: str, uid, name: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO users (platform, uid, name, updated) VALUES (?, ?, ?, ?)",
            (platform, str(uid), name, time.time()),
        )
        self._conn.commit()

    # ---------- 维护 ----------

    def prune(self, days: int) -> int:
        cutoff = time.time() - days * 86400
        cur = self._conn.execute("DELETE FROM seen WHERE first_seen_ts < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    # ---------- 状态导出 ----------

    def dump_status(self) -> str:
        lines = []
        cur = self._conn.execute(
            "SELECT monitor_id, seeded, fail_count, last_success_ts, last_fail_ts, last_alert_ts FROM monitor_state"
        )
        for mid, seeded, fails, ok_ts, fail_ts, alert_ts in cur.fetchall():
            fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"
            lines.append(
                f"  {mid:<22} 已初始化={bool(seeded)} 连续失败={fails} "
                f"上次成功={fmt(ok_ts)} 上次失败={fmt(fail_ts)} 上次告警={fmt(alert_ts)}"
            )
        n = self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        return "\n".join(lines) + f"\n  去重库已记录 {n} 条消息"

    def kv_get(self, key: str, default=None):
        cur = self._conn.execute("SELECT v FROM kv WHERE k = ?", (key,)) if self._table_kv() else None
        if cur is not None:
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
        return default

    def kv_set(self, key: str, value) -> None:
        if not self._table_kv():
            self._conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            self._conn.commit()
        self._conn.execute(
            "INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False))
        )
        self._conn.commit()

    def _table_kv(self) -> bool:
        cur = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kv'")
        return cur.fetchone() is not None
