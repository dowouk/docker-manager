"""Server-side sessions: random token in a cookie, data in SQLite.

Only the SHA-256 of the token is stored, so a copy of the database cannot be
used to sign in. Lifetime is enforced here (8 hours idle, 7 days absolute), not
by the cookie, which is why it carries no Max-Age.
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
_COOKIE_ATTRS = "Path=/; HttpOnly; Secure; SameSite=Lax"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionStore:
    def __init__(self, store: Store, clock: Callable[[], datetime] = _utcnow):
        self._store = store
        self._clock = clock

    def create(self, data: dict) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        self._store.session_put(_hash(token), data, now, now)
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
        self._store.session_put(key, data, created_at, now)
        return data

    def save(self, token: str, data: dict) -> None:
        key = _hash(token)
        now = self._clock()
        row = self._store.session_get(key)
        created_at = row[1] if row else now
        self._store.session_put(key, data, created_at, now)

    def delete(self, token: str) -> None:
        self._store.session_delete(_hash(token))

    def purge_expired(self) -> int:
        now = self._clock()
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

    def _persist(self, token: str | None, original: dict, session: dict) -> str | None:
        """Write the session back; return a Set-Cookie value if the cookie changes."""
        if session == original:
            return None
        if session.get("user") != original.get("user"):
            # Sign-in or sign-out: never carry a token across it (fixation defence).
            if token:
                self.sessions.delete(token)
            if not session:
                return f"{COOKIE}=; Max-Age=0; {_COOKIE_ATTRS}" if token else None
            return f"{COOKIE}={self.sessions.create(session)}; {_COOKIE_ATTRS}"
        if not session:
            if token:
                self.sessions.delete(token)
                return f"{COOKIE}=; Max-Age=0; {_COOKIE_ATTRS}"
            return None
        if token:
            self.sessions.save(token, session)
            return None
        return f"{COOKIE}={self.sessions.create(session)}; {_COOKIE_ATTRS}"
