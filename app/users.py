"""User accounts: the users table, TOTP and recovery-code state, invites and the
sign-in failure log used for lockouts."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from app.keys import SecretBox
from app.store import Store, _from_iso, _to_iso

INVITE_TTL = timedelta(hours=72)


class LastAdminError(Exception):
    """The change would leave no active administrator."""


class DuplicateUserError(Exception):
    """A user of that kind with that email already exists."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class UserRecord:
    id: int
    kind: str
    email: str
    name: str
    password_hash: str | None
    totp_secret: str | None  # encrypted; use UserStore.totp_secret to read it
    totp_enabled: bool
    totp_last_step: int | None
    recovery_codes: list[str]
    role: str
    role_pinned: bool
    disabled: bool
    must_change_password: bool
    entra_tid: str | None
    entra_oid: str | None
    created_at: datetime
    last_sign_in: datetime | None

    @property
    def active(self) -> bool:
        return not self.disabled


_COLS = ("id, kind, email, name, password_hash, totp_secret, totp_enabled, totp_last_step, "
         "recovery_codes, role, role_pinned, disabled, must_change_password, entra_tid, "
         "entra_oid, created_at, last_sign_in")

_BOOLS = {"totp_enabled", "role_pinned", "disabled", "must_change_password"}
# Columns update() may write. Secrets and credentials (password_hash, totp_*, recovery_codes)
# are written only by their dedicated methods.
_UPDATABLE = {"kind", "email", "name", "role", "role_pinned", "disabled",
              "must_change_password", "entra_tid", "entra_oid", "last_sign_in"}
_INTERNAL = _UPDATABLE | {"password_hash", "totp_secret", "totp_enabled",
                          "totp_last_step", "recovery_codes"}
ROLES = ("pending", "viewer", "operator", "admin")


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")


def _is_unique(exc: sqlite3.IntegrityError) -> bool:
    return "UNIQUE" in str(exc)


def _record(r) -> UserRecord:
    return UserRecord(
        id=r[0], kind=r[1], email=r[2], name=r[3], password_hash=r[4], totp_secret=r[5],
        totp_enabled=bool(r[6]), totp_last_step=r[7], recovery_codes=json.loads(r[8]),
        role=r[9], role_pinned=bool(r[10]), disabled=bool(r[11]),
        must_change_password=bool(r[12]), entra_tid=r[13], entra_oid=r[14],
        created_at=_from_iso(r[15]), last_sign_in=_from_iso(r[16]),
    )


class UserStore:
    def __init__(self, store: Store, box: SecretBox,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._box = box
        self._clock = clock

    # --- reads -------------------------------------------------------------

    def _one(self, where: str, params: tuple) -> UserRecord | None:
        with self._store._lock:
            row = self._store._db.execute(
                f"SELECT {_COLS} FROM users WHERE {where}", params).fetchone()
        return _record(row) if row else None

    def count(self) -> int:
        with self._store._lock:
            return self._store._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def get(self, user_id: int) -> UserRecord | None:
        return self._one("id = ?", (user_id,))

    def by_email(self, kind: str, email: str) -> UserRecord | None:
        return self._one("kind = ? AND lower(email) = lower(?)", (kind, email.strip()))

    def by_entra(self, tid: str, oid: str) -> UserRecord | None:
        return self._one("entra_tid = ? AND entra_oid = ?", (tid, oid))

    def list(self) -> list[UserRecord]:
        with self._store._lock:
            rows = self._store._db.execute(
                f"SELECT {_COLS} FROM users ORDER BY (role = 'admin') DESC, "
                "lower(name), id").fetchall()
        return [_record(r) for r in rows]

    # --- create ------------------------------------------------------------

    def _insert(self, db, kind, email, name, role, pinned, password_hash,
                must_change_password, entra_tid=None, entra_oid=None) -> int:
        _check_role(role)
        try:
            cur = db.execute(
                "INSERT INTO users (kind, email, name, password_hash, role, role_pinned, "
                "must_change_password, entra_tid, entra_oid, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, email.strip(), name, password_hash, role, int(pinned),
                 int(must_change_password), entra_tid, entra_oid, _to_iso(self._clock())),
            )
        except sqlite3.IntegrityError as exc:
            if not _is_unique(exc):
                raise
            raise DuplicateUserError(email.strip()) from exc
        return cur.lastrowid

    def create(self, kind: str, email: str, name: str, role: str, *, pinned: bool = False,
               password_hash: str | None = None,
               must_change_password: bool = False, entra_tid: str | None = None,
               entra_oid: str | None = None) -> UserRecord:
        with self._store.write_tx() as db:
            uid = self._insert(db, kind, email, name, role, pinned, password_hash,
                               must_change_password, entra_tid, entra_oid)
        return self.get(uid)

    def create_first_admin(self, kind: str, email: str, name: str, role: str = "admin", *,
                           pinned: bool = False, password_hash: str | None = None,
                           must_change_password: bool = False, entra_tid: str | None = None,
                           entra_oid: str | None = None) -> UserRecord | None:
        """Insert only if there are no users, checked inside the write transaction so two
        threads or processes can't both win. None if someone else got there first."""
        _check_role(role)
        with self._store.write_tx() as db:
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
                return None
            uid = self._insert(db, kind, email, name, role, pinned, password_hash,
                               must_change_password, entra_tid, entra_oid)
        return self.get(uid)

    # --- update / delete ---------------------------------------------------

    @staticmethod
    def _other_active_admins(db, user_id: int) -> int:
        return db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
            (user_id,),
        ).fetchone()[0]

    def update(self, user_id: int, **fields) -> UserRecord:
        return self._update(user_id, _UPDATABLE, fields)

    def _update(self, user_id: int, allowed: set[str], fields: dict) -> UserRecord:
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"can't update {sorted(unknown)}")
        if "role" in fields:
            _check_role(fields["role"])
        with self._store.write_tx() as db:
            row = db.execute(f"SELECT {_COLS} FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise KeyError(user_id)
            current = _record(row)
            new_role = fields.get("role", current.role)
            new_disabled = bool(fields.get("disabled", current.disabled))
            was_active_admin = current.role == "admin" and not current.disabled
            if (was_active_admin and not (new_role == "admin" and not new_disabled)
                    and self._other_active_admins(db, user_id) == 0):
                raise LastAdminError("there must be at least one active admin")
            values = {}
            for k, v in fields.items():
                if k in _BOOLS:
                    v = int(bool(v))
                elif k == "recovery_codes":
                    v = json.dumps(v)
                elif k == "email":
                    v = v.strip()
                elif k == "last_sign_in" and isinstance(v, datetime):
                    v = _to_iso(v)
                values[k] = v
            if values:
                assignments = ", ".join(f"{k} = ?" for k in values)
                try:
                    db.execute(f"UPDATE users SET {assignments} WHERE id = ?",
                               (*values.values(), user_id))
                except sqlite3.IntegrityError as exc:
                    if not _is_unique(exc):
                        raise
                    raise DuplicateUserError(str(fields.get("email", ""))) from exc
        return self.get(user_id)

    def delete(self, user_id: int) -> None:
        with self._store.write_tx() as db:
            row = db.execute(
                "SELECT role, disabled FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                return
            if (row[0] == "admin" and not row[1]
                    and self._other_active_admins(db, user_id) == 0):
                raise LastAdminError("there must be at least one active admin")
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            db.execute("DELETE FROM invites WHERE user_id = ?", (user_id,))
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # --- TOTP and recovery codes -------------------------------------------

    def set_totp(self, user_id: int, secret: str | None, enabled: bool) -> None:
        encrypted = None if secret is None else self._box.encrypt(secret)
        self._update(user_id, _INTERNAL, dict(
            totp_secret=encrypted, totp_enabled=enabled, totp_last_step=None))

    def totp_secret(self, user: UserRecord) -> str | None:
        if not user.totp_secret:
            return None
        return self._box.decrypt(user.totp_secret)

    def set_totp_last_step(self, user_id: int, step: int) -> bool:
        """Compare-and-set: False if this step (or a later one) was already used, so two
        concurrent sign-ins can't both spend the same code."""
        with self._store.write_tx() as db:
            cur = db.execute(
                "UPDATE users SET totp_last_step = ? WHERE id = ? "
                "AND (totp_last_step IS NULL OR totp_last_step < ?)", (step, user_id, step))
            return cur.rowcount > 0

    def seal(self, text: str) -> str:
        """Encrypt short-lived text (e.g. an unconfirmed TOTP secret kept in a session)."""
        return self._box.encrypt(text)

    def unseal(self, token: str) -> str | None:
        return self._box.decrypt(token) if isinstance(token, str) else None

    def set_password_hash(self, user_id: int, password_hash: str,
                          must_change_password: bool) -> None:
        self._update(user_id, _INTERNAL, dict(
            password_hash=password_hash, must_change_password=must_change_password))

    def set_recovery_codes(self, user_id: int, hashes: list[str]) -> None:
        self._update(user_id, _INTERNAL, dict(recovery_codes=list(hashes)))

    def use_recovery_code(self, user_id: int, code_hash: str) -> bool:
        with self._store.write_tx() as db:
            row = db.execute(
                "SELECT recovery_codes FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                return False
            codes = json.loads(row[0])
            if code_hash not in codes:
                return False
            codes.remove(code_hash)
            db.execute("UPDATE users SET recovery_codes = ? WHERE id = ?",
                       (json.dumps(codes), user_id))
            return True

    # --- Entra and sign-in -------------------------------------------------

    def bind_entra(self, user_id: int, tid: str, oid: str) -> bool:
        """Attach a Microsoft identity to an account that has none yet. False if the
        account is gone or already bound (compare-and-set). Raises DuplicateUserError if
        the identity is bound to another account."""
        try:
            with self._store.write_tx() as db:
                cur = db.execute(
                    "UPDATE users SET entra_tid = ?, entra_oid = ? "
                    "WHERE id = ? AND entra_oid IS NULL", (tid, oid, user_id))
                return cur.rowcount == 1
        except sqlite3.IntegrityError as exc:
            if not _is_unique(exc):
                raise
            raise DuplicateUserError(oid) from exc

    def touch_sign_in(self, user_id: int) -> None:
        self.update(user_id, last_sign_in=self._clock())

    # --- invites -----------------------------------------------------------

    def create_invite(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._store.write_tx() as db:
            db.execute("INSERT INTO invites (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                       (_hash(token), user_id, _to_iso(self._clock() + INVITE_TTL)))
        return token

    def use_invite(self, token: str) -> UserRecord | None:
        with self._store.write_tx() as db:
            row = db.execute("SELECT user_id, expires_at FROM invites WHERE token_hash = ?",
                             (_hash(token),)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM invites WHERE token_hash = ?", (_hash(token),))
            if _from_iso(row[1]) <= self._clock():
                return None
            user_id = row[0]
        return self.get(user_id)

    # --- failures ----------------------------------------------------------

    def record_failure(self, scope: Literal["account", "ip", "setup", "account-lock", "ip-lock"], key: str) -> None:
        with self._store._lock, self._store._db:
            self._store._db.execute(
                "INSERT INTO auth_failures (scope, key, at) VALUES (?, ?, ?)",
                (scope, key, _to_iso(self._clock())))

    def failures(self, scope: str, key: str, within: timedelta) -> int:
        with self._store._lock:
            return self._store._db.execute(
                "SELECT COUNT(*) FROM auth_failures WHERE scope = ? AND key = ? AND at >= ?",
                (scope, key, _to_iso(self._clock() - within))).fetchone()[0]

    def clear_failures(self, scope: str, key: str) -> None:
        with self._store._lock, self._store._db:
            self._store._db.execute(
                "DELETE FROM auth_failures WHERE scope = ? AND key = ?", (scope, key))

    def purge_failures(self, older_than: timedelta) -> None:
        with self._store._lock, self._store._db:
            self._store._db.execute(
                "DELETE FROM auth_failures WHERE at < ?",
                (_to_iso(self._clock() - older_than),))

    # --- sessions ----------------------------------------------------------

    def end_sessions(self, user_id: int, keep_token_hash: str | None = None) -> int:
        """Delete the user's sessions, optionally sparing one (by token hash)."""
        return self._store.session_delete_for_user(user_id, keep_token_hash)

    def now(self) -> datetime:
        return self._clock()
