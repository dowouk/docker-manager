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

    async def read(request: Request):
        return JSONResponse(request.session)

    app = Starlette(routes=[
        Route("/set", set_user), Route("/touch", touch),
        Route("/clear", clear), Route("/read", read),
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
    assert "max-age" not in header.lower()
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
