"""SQLite storage. One writer (the poller) and many readers (the web API); WAL mode
keeps them out of each other's way."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .models import Reading, SiteInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id            INTEGER PRIMARY KEY,
    account_id    TEXT NOT NULL,
    platform      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    capacity_kwp  REAL,
    timezone      TEXT,
    meta          TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    last_error    TEXT,
    UNIQUE(account_id, external_id)
);
CREATE TABLE IF NOT EXISTS readings (
    id               INTEGER PRIMARY KEY,
    site_id          INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    fetched_at       REAL NOT NULL,
    reported_at      REAL,
    pv_w REAL, load_w REAL, battery_w REAL, grid_w REAL, soc REAL,
    pv_kwh_today REAL, load_kwh_today REAL, import_kwh_today REAL, export_kwh_today REAL,
    device_status    TEXT
);
CREATE INDEX IF NOT EXISTS readings_site_time ON readings(site_id, fetched_at);
CREATE TABLE IF NOT EXISTS latest_raw (
    site_id    INTEGER PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
    fetched_at REAL NOT NULL,
    raw        TEXT
);
"""

READING_COLS = ["pv_w", "load_w", "battery_w", "grid_w", "soc", "pv_kwh_today", "load_kwh_today",
                "import_kwh_today", "export_kwh_today"]


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # -- writes -----------------------------------------------------------------
    def upsert_site(self, account_id: str, platform: str, s: SiteInfo, error: Optional[str] = None) -> int:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO sites(account_id, platform, external_id, name, capacity_kwp, timezone, meta,
                                     first_seen, last_seen, last_error)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, external_id) DO UPDATE SET
                     platform=excluded.platform, name=excluded.name,
                     capacity_kwp=COALESCE(excluded.capacity_kwp, sites.capacity_kwp),
                     timezone=COALESCE(excluded.timezone, sites.timezone),
                     meta=excluded.meta, last_seen=excluded.last_seen, last_error=excluded.last_error""",
                (account_id, platform, s.external_id, s.name, s.capacity_kwp, s.timezone,
                 json.dumps(s.meta or {}), now, now, error))
            row = self._conn.execute("SELECT id FROM sites WHERE account_id=? AND external_id=?",
                                     (account_id, s.external_id)).fetchone()
        return row["id"]

    def add_reading(self, site_id: int, r: Reading, fetched_at: Optional[float] = None) -> None:
        t = fetched_at or time.time()
        with self._lock:
            self._conn.execute(
                f"""INSERT INTO readings(site_id, fetched_at, reported_at, {', '.join(READING_COLS)}, device_status)
                    VALUES (?,?,?,{','.join('?' * len(READING_COLS))},?)""",
                (site_id, t, r.reported_at, *[getattr(r, c) for c in READING_COLS], r.device_status))
            if r.raw is not None:
                self._conn.execute("INSERT OR REPLACE INTO latest_raw(site_id, fetched_at, raw) VALUES (?,?,?)",
                                   (site_id, t, json.dumps(r.raw, default=str)))

    def prune(self, retention_days: float) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            return self._conn.execute("DELETE FROM readings WHERE fetched_at < ?", (cutoff,)).rowcount

    def delete_account_sites_except(self, valid_account_ids: list[str]) -> None:
        """Hide sites from accounts removed from the config (their history is kept)."""
        marks = ",".join("?" * len(valid_account_ids)) or "''"
        with self._lock:
            self._conn.execute(f"UPDATE sites SET last_error='account removed from config' "
                               f"WHERE account_id NOT IN ({marks})", tuple(valid_account_ids))

    # -- reads ------------------------------------------------------------------
    def account_has_readings(self, account_id: str) -> bool:
        return bool(self._q("""SELECT 1 FROM readings r JOIN sites s ON s.id = r.site_id
                               WHERE s.account_id = ? LIMIT 1""", (account_id,)))

    def sites_with_latest(self, account_ids: list[str]) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(account_ids)) or "''"
        rows = self._q(f"""
            SELECT s.*, r.fetched_at, r.reported_at, {', '.join('r.' + c for c in READING_COLS)}, r.device_status
            FROM sites s
            LEFT JOIN readings r ON r.id = (SELECT id FROM readings WHERE site_id = s.id
                                            ORDER BY fetched_at DESC LIMIT 1)
            WHERE s.account_id IN ({marks})
            ORDER BY s.name COLLATE NOCASE""", tuple(account_ids))
        return [_site_row(r) for r in rows]

    def site(self, site_id: int) -> Optional[dict[str, Any]]:
        rows = self._q(f"""
            SELECT s.*, r.fetched_at, r.reported_at, {', '.join('r.' + c for c in READING_COLS)}, r.device_status
            FROM sites s
            LEFT JOIN readings r ON r.id = (SELECT id FROM readings WHERE site_id = s.id
                                            ORDER BY fetched_at DESC LIMIT 1)
            WHERE s.id = ?""", (site_id,))
        return _site_row(rows[0]) if rows else None

    def history(self, site_id: int, since: float, max_points: int = 600) -> list[dict[str, Any]]:
        rows = self._q(f"""SELECT fetched_at, {', '.join(READING_COLS)} FROM readings
                           WHERE site_id=? AND fetched_at>=? ORDER BY fetched_at""", (site_id, since))
        step = max(1, len(rows) // max_points)
        return [dict(r) for r in rows[::step]]

    def raw(self, site_id: int) -> Optional[dict[str, Any]]:
        rows = self._q("SELECT fetched_at, raw FROM latest_raw WHERE site_id=?", (site_id,))
        if not rows:
            return None
        return {"fetched_at": rows[0]["fetched_at"], "raw": json.loads(rows[0]["raw"] or "null")}


def _site_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d
