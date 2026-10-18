"""Server-side sessions: random token in a cookie, data in SQLite.

Only the SHA-256 of the token is stored, so a copy of the database cannot be
used to sign in. Lifetime is enforced here (8 hours idle, 7 days absolute), not
by the cookie. The cookie's Max-Age is the absolute lifetime, so it survives the
browser closing (0.1 kept 8 hours).
"""
import copy
import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection

from app.store import Store

SESSION_IDLE = timedelta(hours=8)
SESSION_MAX = timedelta(days=7)
COOKIE = "session"
PURGE_EVERY = timedelta(minutes=10)
_COOKIE_ATTRS = "Path=/; HttpOnly; Secure; SameSite=Lax"
_COOKIE_MAX_AGE = int(SESSION_MAX.total_seconds())


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionStore:
    def __init__(self, store: Store, clock: Callable[[], datetime] = _utcnow):
        self._store = store
        self._clock = clock
        self._last_purge = clock()

    def create(self, data: dict) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        if now - self._last_purge >= PURGE_EVERY:
            # Anonymous /auth/login creates rows; don't wait for the hourly job.
            self.purge_expired()
        self._store.session_put(_hash(token), data, now, now, data.get("user_id"))
        return token

    def load(self, token: str) -> dict | None:
        key = _hash(token)
        row = self._store.session_get(key)
        if row is None:
            return None
        data, created_at, last_seen = row
        now = self._clock()
        if now - last_seen > SESSION_IDLE or now - created_at > SESSION_MAX:
            self._store.session_delete(key)
            return None
        if not self._store.session_touch(key, now):
            return None  # deleted since we read it
        return data

    def save(self, token: str, data: dict) -> bool:
        """Update an existing session; False (nothing written) if it no longer exists."""
        return self._store.session_update(_hash(token), data, self._clock(),
                                          data.get("user_id"))

    def delete(self, token: str) -> None:
        self._store.session_delete(_hash(token))

    def purge_expired(self) -> int:
        now = self._clock()
        self._last_purge = now
        return self._store.session_purge(now - SESSION_IDLE, now - SESSION_MAX)


class ServerSessionMiddleware:
    """Expose the session as ``scope["session"]`` and persist changes afterwards."""

    def __init__(self, app, sessions: SessionStore):
        self.app = app
        self.sessions = sessions

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        token = HTTPConnection(scope).cookies.get(COOKIE)
        loaded = self.sessions.load(token) if token else None
        if loaded is None:
            token = None  # unknown, expired or garbage: treat as no session
        original = copy.deepcopy(loaded) if loaded is not None else {}
        session = copy.deepcopy(original)
        scope["session"] = session

        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                cookie = self._persist(token, original, session)
                if cookie:
                    MutableHeaders(scope=message).append("set-cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _new_cookie(self, session: dict) -> str:
        return (f"{COOKIE}={self.sessions.create(session)}; "
                f"Max-Age={_COOKIE_MAX_AGE}; {_COOKIE_ATTRS}")

    def _persist(self, token: str | None, original: dict, session: dict) -> str | None:
        """Write the session back; return a Set-Cookie value if the cookie changes."""
        if session == original:
            return None
        if session.get("user_id") != original.get("user_id"):
            # Sign-in or sign-out: never carry a token across it (fixation defence).
            if token:
                self.sessions.delete(token)
            if not session:
                return f"{COOKIE}=; Max-Age=0; {_COOKIE_ATTRS}" if token else None
            return self._new_cookie(session)
        if not session:
            if token:
                self.sessions.delete(token)
                return f"{COOKIE}=; Max-Age=0; {_COOKIE_ATTRS}"
            return None
        if token:
            if self.sessions.save(token, session):
                return None
            # Revoked mid-request: do not resurrect it.
            return f"{COOKIE}=; Max-Age=0; {_COOKIE_ATTRS}"
        return self._new_cookie(session)
