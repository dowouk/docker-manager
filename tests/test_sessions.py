"""Server-side session middleware and store."""
from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.sessions import ServerSessionMiddleware, SessionStore
from app.store import Store


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    clock = Clock()
    sessions = SessionStore(store, clock)

    async def set_user(request: Request):
        request.session["user"] = {"email": "a@example.com"}
        return JSONResponse({})

    async def touch(request: Request):
        request.session["x"] = 1
        return JSONResponse({})

    async def clear(request: Request):
        request.session.clear()
        return JSONResponse({})

    async def revoked(request: Request):
        sessions.delete(request.cookies["session"])  # e.g. signed out elsewhere
        request.session["x"] = 1
        return JSONResponse({})

    async def read(request: Request):
        return JSONResponse(request.session)

    app = Starlette(routes=[
        Route("/set", set_user), Route("/touch", touch),
        Route("/clear", clear), Route("/revoked", revoked), Route("/read", read),
    ])
    app.add_middleware(ServerSessionMiddleware, sessions=sessions)
    client = TestClient(app, base_url="https://testserver")
    return client, store, clock, sessions


def _rows(store):
    return store._db.execute("SELECT token_hash, data FROM sessions").fetchall()


def test_sign_in_sets_cookie_and_stores_only_hash(env):
    client, store, _, _ = env
    r = client.get("/set")
    header = r.headers["set-cookie"]
    token = client.cookies["session"]
    assert header.startswith(f"session={token};")
    for attr in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attr.lower() in header.lower()
    assert "max-age=604800" in header.lower()  # the 7 day absolute lifetime
    assert all(token not in "".join(map(str, row)) for row in _rows(store))
    assert client.get("/read").json() == {"user": {"email": "a@example.com"}}


def test_rotation_on_user_change(env):
    client, _, _, sessions = env
    client.get("/set")
    a = client.cookies["session"]
    client.get("/clear")
    client.get("/set")
    b = client.cookies["session"]
    assert a != b
    assert sessions.load(a) is None
    assert sessions.load(b) is not None


def test_rotation_when_cleared_and_reset_in_one_request(env):
    client, _, _, sessions = env
    client.get("/touch")
    a = client.cookies["session"]
    assert sessions.load(a) == {"x": 1}
    client.get("/set")  # user None -> value: new token even though a session existed
    assert client.cookies["session"] != a
    assert sessions.load(a) is None


def test_idle_timeout(env):
    client, _, clock, _ = env
    client.get("/set")
    clock.advance(hours=8, minutes=1)
    assert client.get("/read").json() == {}


def test_activity_within_idle_window_keeps_session(env):
    client, _, clock, _ = env
    client.get("/set")
    clock.advance(hours=7)
    assert client.get("/read").json() != {}
    clock.advance(hours=7)
    assert client.get("/read").json() != {}


def test_absolute_timeout(env):
    client, _, clock, _ = env
    client.get("/set")
    for _ in range(7 * 24):
        clock.advance(hours=1)
        assert client.get("/touch").status_code == 200
    assert client.get("/read").json() != {}
    clock.advance(minutes=1)
    assert client.get("/read").json() == {}


@pytest.mark.parametrize("value", ["abc", "eyJ1c2VyIjoie30ifQ==.aaaa.bbbb"])
def test_unknown_or_garbage_cookie_is_empty_session(env, value):
    client, _, _, _ = env
    client.cookies.set("session", value, domain="testserver.local", path="/")
    r = client.get("/read")
    assert r.status_code == 200 and r.json() == {}


def test_clear_deletes_row_and_expires_cookie(env):
    client, store, _, _ = env
    client.get("/set")
    assert len(_rows(store)) == 1
    r = client.get("/clear")
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert _rows(store) == []


def test_purge_expired(env):
    client, store, clock, sessions = env
    client.get("/set")
    assert sessions.purge_expired() == 0
    clock.advance(hours=9)
    assert sessions.purge_expired() == 1
    assert _rows(store) == []


def test_session_revoked_mid_request_is_not_resurrected(env):
    client, store, _, sessions = env
    client.get("/set")
    token = client.cookies["session"]
    r = client.get("/revoked")
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert sessions.load(token) is None
    assert _rows(store) == []


def test_create_purges_expired_rows_at_most_every_ten_minutes(env):
    _, store, clock, sessions = env
    sessions.create({"a": 1})
    clock.advance(hours=9)  # row a is now expired
    sessions.create({"b": 1})  # over 10 minutes since the last purge: a is purged
    assert len(_rows(store)) == 1
    clock.advance(hours=9)  # b is now expired, but the last purge was only just before
    sessions._last_purge = clock.now  # noqa: SLF001
    sessions.create({"c": 1})
    assert len(_rows(store)) == 2  # b kept: no purge due yet
    clock.advance(minutes=11)
    sessions.create({"d": 1})
    assert len(_rows(store)) == 2  # b purged; c is still live


def test_startup_purges_sessions(settings, tmp_path, monkeypatch):
    import httpx

    from app.main import create_app

    calls = []
    monkeypatch.setattr(SessionStore, "purge_expired", lambda self: calls.append(1) or 0)
    create_app(settings, docker_client=object(), http=httpx.AsyncClient(),
               store=Store(str(tmp_path / "x" / "dashboard.db")), start_scheduler=False)
    assert calls == [1]
