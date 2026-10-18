"""Reusable auth test helpers (also used against the full app in Task 13)."""
import re

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles
from starlette.testclient import WebSocketDisconnect
from starlette.routing import Mount, Route, WebSocketRoute

CSRF = "test-csrf-token"
USER = {"name": "Test User", "email": "test@example.com"}


def set_session(client: TestClient, app: FastAPI, data: dict) -> None:
    token = app.state.sessions.create(data)
    # domain/path must match what the app's Set-Cookie uses, or the jar keeps two cookies
    client.cookies.set("session", token, domain="testserver.local", path="/")


def logged_in_client(app: FastAPI, settings=None, **kwargs) -> TestClient:
    client = TestClient(app, base_url="https://testserver", **kwargs)
    set_session(client, app, {"user": USER, "csrf": CSRF})
    return client


EXPECTED_AUTH_ROUTES = {
    ("/auth/login", "GET"),
    ("/auth/callback", "GET"),
    ("/auth/logout", "POST"),
    ("/auth/signed-out", "GET"),
}


def assert_all_routes_guarded(app: FastAPI) -> None:
    """Every route outside /auth/ and /static/ must refuse anonymous requests.

    Also asserts the /auth/* route set is exactly the expected one, so a stray
    route under the public prefix cannot become public unnoticed.
    """
    auth_routes: set[tuple[str, str]] = set()
    checked = 0
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
    assert auth_routes == EXPECTED_AUTH_ROUTES, f"unexpected /auth routes: {auth_routes}"
    assert checked, "no routes were checked"
