"""Reusable auth test helpers (also used against the full app in Task 13)."""
import re

from app.keys import KeyUnusable
from app.roles import Role
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles
from starlette.testclient import WebSocketDisconnect
from starlette.routing import Mount, Route, WebSocketRoute

CSRF = "test-csrf-token"
EMAIL = "test@example.com"


TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _give_totp(users, rec):
    """Local Admins need two-step on (require_totp_admins) or the guard sends every
    test to the enrolment page."""
    try:
        users.set_totp(rec.id, TEST_TOTP_SECRET, True)
    except KeyUnusable:  # a test with a deliberately broken key file: flag only
        with users._store.write_tx() as db:
            db.execute("UPDATE users SET totp_enabled = 1 WHERE id = ?", (rec.id,))


def make_user(app: FastAPI, role: Role = Role.ADMIN, email: str = EMAIL, name: str = "Test User"):
    """The local test user with the given role (created, or its role changed if it exists)."""
    users = app.state.users
    rec = users.by_email("local", email)
    if rec is None:
        rec = users.create("local", email, name, role.value)
    elif rec.role != role.value:
        # direct write: the last-admin rule is not what these tests are about
        with users._store.write_tx() as db:
            db.execute("UPDATE users SET role = ? WHERE id = ?", (role.value, rec.id))
    if role == Role.ADMIN and not users.get(rec.id).totp_enabled:
        _give_totp(users, rec)
    return users.get(rec.id)


def set_session(client: TestClient, app: FastAPI, data: dict) -> None:
    token = app.state.sessions.create(data)
    # domain/path must match what the app's Set-Cookie uses, or the jar keeps two cookies
    client.cookies.set("session", token, domain="testserver.local", path="/")


def sign_in(client: TestClient, app: FastAPI, role: Role = Role.ADMIN, **user_kw):
    rec = make_user(app, role, **user_kw)
    set_session(client, app, {"user_id": rec.id, "csrf": CSRF})
    return rec


def logged_in_client(app: FastAPI, role: Role = Role.ADMIN, **kwargs) -> TestClient:
    client = TestClient(app, base_url="https://testserver", **kwargs)
    sign_in(client, app, role)
    return client


EXPECTED_AUTH_ROUTES = {
    ("/auth/login", "GET"),
    ("/auth/login", "POST"),
    ("/auth/totp", "GET"),
    ("/auth/totp", "POST"),
    ("/auth/entra", "GET"),
    ("/auth/callback", "GET"),
    ("/auth/logout", "POST"),
    ("/auth/signed-out", "GET"),
}


# What setup_auth alone registers (test apps that don't go through create_app).
BARE_AUTH_ROUTES = EXPECTED_AUTH_ROUTES - {
    ("/auth/entra", "GET"), ("/auth/callback", "GET"),
    ("/auth/login", "GET"), ("/auth/login", "POST"), ("/auth/totp", "GET"), ("/auth/totp", "POST"),
}


def assert_all_routes_guarded(app: FastAPI, expected_auth: set | None = None) -> None:
    """Every route outside /auth/, /static/ and /healthz must refuse anonymous requests.

    /healthz must answer 200/503 and never redirect; /setup* is skipped only while
    ``app.state.setup.is_open()`` is true.

    Also asserts the /auth/* route set is exactly the expected one, so a stray
    route under the public prefix cannot become public unnoticed.
    """
    auth_routes: set[tuple[str, str]] = set()
    checked = 0
    setup_open = bool(getattr(getattr(app.state, "setup", None), "is_open", lambda: False)())
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        norm = path.rstrip("/") + "/"
        if norm.startswith("/auth/"):
            for method in getattr(route, "methods", None) or {"WS"}:
                if method not in ("HEAD", "OPTIONS"):
                    auth_routes.add((path, method))
            continue
        if norm.startswith("/static/"):
            assert isinstance(route, Mount) and isinstance(route.app, StaticFiles), (
                f"{path} is under /static/ (public) but is not a StaticFiles mount"
            )
            continue
        if norm == "/healthz/":
            with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
                for method in ("GET", "HEAD"):
                    r = c.request(method, "/healthz", headers={"Accept": "text/html"})
                    assert r.status_code in (200, 503), f"/healthz returned {r.status_code}"
            checked += 1
            continue
        if (norm == "/setup/" or norm.startswith("/setup/")) and setup_open:
            continue  # public only while setup is open
        if isinstance(route, Mount):
            path = path.rstrip("/") + "/x"
        elif not isinstance(route, (Route, APIRoute, WebSocketRoute)):
            continue
        concrete = re.sub(r"\{[^}]*\}", "1", path)
        if isinstance(route, WebSocketRoute):
            with TestClient(app, base_url="https://testserver") as c:
                try:
                    with c.websocket_connect(concrete):
                        pass
                except WebSocketDisconnect as exc:
                    assert exc.code == 1008, f"WS {concrete} closed with {exc.code}, not 1008"
                else:
                    raise AssertionError(f"WS {concrete} accepted an unauthenticated connection")
            checked += 1
            continue
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                for accept in ("text/html", "application/json", "text/event-stream"):
                    r = c.request(method, concrete, headers={"Accept": accept})
                    assert r.status_code in (302, 401), (
                        f"{method} {concrete} ({accept}) returned {r.status_code} unauthenticated"
                    )
                    if r.status_code == 302:
                        assert r.headers["location"] == "/auth/login"
        checked += 1
    assert auth_routes == (EXPECTED_AUTH_ROUTES if expected_auth is None else expected_auth), f"unexpected /auth routes: {auth_routes}"
    assert checked, "no routes were checked"
