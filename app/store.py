import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

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


@dataclass
class AuditEntry:
    id: int
    at: datetime
    user: str
    kind: str
    summary: str


def _migration_1(db: sqlite3.Connection) -> None:
    # The 0.1 schema; IF NOT EXISTS so an existing 0.1 database is adopted as is.
    db.execute(
        "CREATE TABLE IF NOT EXISTS check_results ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "checked_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
        "target TEXT NOT NULL, user TEXT NOT NULL, status TEXT NOT NULL, "
        "started_at TEXT, finished_at TEXT, log TEXT NOT NULL DEFAULT '')"
    )


def _migration_2(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, data TEXT NOT NULL, "
        "created_at TEXT NOT NULL, last_seen TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
        "user TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL)"
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_migration_1, _migration_2]


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
        with self._lock:
            self._migrate()

    def _table_exists(self, name: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    def _migrate(self) -> None:
        # Judged before migration 1, which would create the 0.1 tables on a fresh database.
        upgraded = not self._table_exists("schema_version") and self._table_exists("jobs")
        self._db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self._db.execute("SELECT version FROM schema_version").fetchone()
        current = row[0] if row else 0
        for number, migration in enumerate(MIGRATIONS, start=1):
            if number <= current:
                continue
            self._db.execute("BEGIN")
            try:
                migration(self._db)
                if number == len(MIGRATIONS) and upgraded:
                    self._db.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES ('upgraded_from_0_1', '1')"
                    )
                self._db.execute("DELETE FROM schema_version")
                self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def load_settings(self) -> dict[str, object]:
        with self._lock:
            rows = self._db.execute("SELECT name, value FROM settings").fetchall()
        return {name: json.loads(value) for name, value in rows}

    def save_settings(self, values: dict[str, object]) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO settings (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                [(name, json.dumps(value)) for name, value in values.items()],
            )

    def delete_setting(self, name: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM settings WHERE name = ?", (name,))

    def add_audit(self, user: str, kind: str, summary: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO audit (at, user, kind, summary) VALUES (?, ?, ?, ?)",
                (_now(), user, kind, summary),
            )

    def list_audit(self, limit: int = 100) -> list[AuditEntry]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, at, user, kind, summary FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [AuditEntry(r[0], _from_iso(r[1]), r[2], r[3], r[4]) for r in rows]

    def session_get(self, token_hash: str) -> tuple[dict, datetime, datetime] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data, created_at, last_seen FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), _from_iso(row[1]), _from_iso(row[2])

    def session_put(self, token_hash: str, data: dict,
                    created_at: datetime, last_seen: datetime) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions (token_hash, data, created_at, last_seen) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(token_hash) DO UPDATE SET "
                "data = excluded.data, created_at = excluded.created_at, "
                "last_seen = excluded.last_seen",
                (token_hash, json.dumps(data), _to_iso(created_at), _to_iso(last_seen)),
            )

    def session_delete(self, token_hash: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def session_purge(self, idle_before: datetime, created_before: datetime) -> int:
        # ISO strings in UTC compare correctly as text.
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM sessions WHERE last_seen < ? OR created_at < ?",
                (_to_iso(idle_before), _to_iso(created_before)),
            )
            return cur.rowcount

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
