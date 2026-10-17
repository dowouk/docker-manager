import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

INTERRUPTED_LINE = "Interrupted – the dashboard restarted"
SELF_UPDATED_LINE = "Dashboard restarted – self-update applied"
KEEP_CHECKS = 100


@dataclass
class Job:
    id: int
    kind: str
    target: str
    user: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    log: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Store:
    def __init__(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS check_results ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "checked_at TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                "target TEXT NOT NULL, user TEXT NOT NULL, status TEXT NOT NULL, "
                "started_at TEXT, finished_at TEXT, log TEXT NOT NULL DEFAULT '')"
            )

    def save_check(self, payload: dict, checked_at: datetime) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO check_results (checked_at, payload) VALUES (?, ?)",
                (_to_iso(checked_at), json.dumps(payload)),
            )
            self._db.execute(
                "DELETE FROM check_results WHERE id NOT IN ("
                "SELECT id FROM check_results ORDER BY checked_at DESC, id DESC LIMIT ?)",
                (KEEP_CHECKS,),
            )

    def latest_check(self) -> tuple[datetime, dict] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT checked_at, payload FROM check_results "
                "ORDER BY checked_at DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return _from_iso(row[0]), json.loads(row[1])

    def create_job(self, kind: str, target: str, user: str) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO jobs (kind, target, user, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (kind, target, user, _now()),
            )
            return cur.lastrowid

    def append_log(self, job_id: int, text: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET log = log || ? WHERE id = ?", (text, job_id)
            )

    def finish_job(self, job_id: int, status: Literal["succeeded", "failed"]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                (status, _now(), job_id),
            )

    _COLS = "id, kind, target, user, status, started_at, finished_at, log"

    @staticmethod
    def _job(row) -> Job:
        return Job(row[0], row[1], row[2], row[3], row[4],
                   _from_iso(row[5]), _from_iso(row[6]), row[7])

    def get_job(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {self._COLS} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {self._COLS} FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._job(r) for r in rows]

    def mark_interrupted_jobs(self) -> None:
        with self._lock, self._db:
            # A dashboard self-update always ends with the process restarting.
            self._db.execute(
                "UPDATE jobs SET status = 'succeeded', finished_at = ?, "
                "log = CASE WHEN log = '' OR substr(log, -1) = char(10) "
                "THEN log ELSE log || char(10) END || ? "
                "WHERE status = 'running' AND kind = 'self'",
                (_now(), SELF_UPDATED_LINE + "\n"),
            )
            self._db.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, "
                "log = CASE WHEN log = '' OR substr(log, -1) = char(10) "
                "THEN log ELSE log || char(10) END || ? "
                "WHERE status = 'running'",
                (_now(), INTERRUPTED_LINE + "\n"),
            )
