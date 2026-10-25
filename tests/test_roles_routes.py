"""Minimum role for every route, and what each role sees in the pages."""
import re

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app.roles import Role
from auth_helpers import CSRF, assert_all_routes_guarded, logged_in_client
from test_routes import compose_file, make_app, seeded  # noqa: F401 – fixtures

# Every route that needs a signed-in user, with the lowest role allowed. Add new routes here:
# test_every_route_has_an_entry fails until you do. Role.PENDING means any signed-in user.
ROUTE_ROLES: list[tuple[str, str, Role]] = [
    ("GET", "/", Role.VIEWER),
    ("GET", "/layout", Role.VIEWER),
    ("GET", "/history", Role.VIEWER),
    ("GET", "/history?view=changes", Role.ADMIN),
    ("POST", "/check", Role.OPERATOR),
    ("POST", "/containers/1/update", Role.OPERATOR),
    ("POST", "/containers/1/control", Role.OPERATOR),
    ("GET", "/stacks/1/config", Role.OPERATOR),
    ("GET", "/jobs/1/status", Role.OPERATOR),
    ("POST", "/stacks/1/config", Role.OPERATOR),
    ("GET", "/containers/1/version-preview", Role.OPERATOR),
    ("POST", "/containers/1/version", Role.OPERATOR),
    ("POST", "/containers/1/skip", Role.OPERATOR),
    ("POST", "/skips/1/remove", Role.OPERATOR),
    ("GET", "/containers/1/rules", Role.OPERATOR),
    ("POST", "/containers/1/rules", Role.OPERATOR),
    ("GET", "/jobs/1", Role.OPERATOR),
    ("GET", "/jobs/1/stream", Role.OPERATOR),
    ("POST", "/digest/send", Role.ADMIN),
    ("POST", "/mailcow/update", Role.ADMIN),
    ("POST", "/self/update", Role.ADMIN),
    ("GET", "/mailcow/host-access", Role.ADMIN),
    ("POST", "/mailcow/host-access", Role.ADMIN),
    ("GET", "/settings", Role.ADMIN),
    ("GET", "/settings/channels/new", Role.ADMIN),
    ("GET", "/settings/channels/1", Role.ADMIN),
    ("POST", "/settings/channels", Role.ADMIN),
    ("POST", "/settings/channels/reset-unreadable", Role.ADMIN),
    ("POST", "/settings/channels/1", Role.ADMIN),
    ("POST", "/settings/channels/1/delete", Role.ADMIN),
    ("POST", "/settings/channels/1/test", Role.ADMIN),
    ("POST", "/settings/registries", Role.ADMIN),
    ("POST", "/settings/registries/1/delete", Role.ADMIN),
    ("POST", "/settings/signin/test", Role.ADMIN),
    ("POST", "/settings/group-mappings", Role.ADMIN),
    ("POST", "/settings/group-mappings/1/delete", Role.ADMIN),
    ("POST", "/settings/1", Role.ADMIN),
    ("GET", "/pending", Role.PENDING),
    ("GET", "/account/password", Role.PENDING),
    ("POST", "/account/password", Role.PENDING),
    ("GET", "/account/two-step", Role.PENDING),
    ("POST", "/account/two-step", Role.PENDING),
    ("GET", "/account", Role.PENDING),
    ("GET", "/photos/1", Role.PENDING),  # your own picture; others' are 404 below Admin
    ("POST", "/account/two-step/off", Role.PENDING),
    ("POST", "/account/recovery-codes", Role.PENDING),
    ("POST", "/account/theme", Role.PENDING),
    ("POST", "/account/sign-out-everywhere", Role.PENDING),
    ("GET", "/users", Role.ADMIN),
    ("GET", "/users/1", Role.ADMIN),
    ("POST", "/users/local", Role.ADMIN),
    ("POST", "/users/entra", Role.ADMIN),
    ("POST", "/users/1/role", Role.ADMIN),
    ("POST", "/users/1/unpin", Role.ADMIN),
    ("POST", "/users/1/disable", Role.ADMIN),
    ("POST", "/users/1/enable", Role.ADMIN),
    ("POST", "/users/1/reset-password", Role.ADMIN),
    ("POST", "/users/1/reset-two-step", Role.ADMIN),
    ("POST", "/users/1/sign-out", Role.ADMIN),
    ("POST", "/users/1/delete", Role.ADMIN),
    ("POST", "/users/1/invite", Role.ADMIN),
]

BELOW = {Role.VIEWER: Role.PENDING, Role.OPERATOR: Role.VIEWER, Role.ADMIN: Role.OPERATOR}


def _is_public(path: str) -> bool:
    norm = path.rstrip("/") + "/"
    return norm.startswith(("/auth/", "/static/", "/setup/")) or norm == "/healthz/"


def _app_routes(app) -> set[tuple[str, str]]:
    found = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None or isinstance(route, Mount) or _is_public(path):
            continue
        for method in getattr(route, "methods", None) or ():
            if method not in ("HEAD", "OPTIONS"):
                found.add((method, re.sub(r"\{[^}]*\}", "1", path)))
    return found


def _send(client: TestClient, method: str, path: str):
    return client.request(method, path, headers={"x-csrf-token": CSRF, "accept": "text/html"})


@pytest.fixture
def app(settings, tmp_path, seeded):  # noqa: F811
    return make_app(settings, tmp_path, seeded)


def test_every_route_has_an_entry(app):
    listed = {(m, p.split("?")[0]) for m, p, _ in ROUTE_ROLES}
    assert _app_routes(app) - listed == set(), "add these routes to ROUTE_ROLES"
    assert listed - _app_routes(app) == set(), "ROUTE_ROLES lists routes that don't exist"


def test_all_routes_still_guarded(app):
    assert_all_routes_guarded(app)


@pytest.mark.parametrize("method,path,minimum", ROUTE_ROLES)
def test_route_minimum_role(app, method, path, minimum):
    with logged_in_client(app, minimum, follow_redirects=False) as at:
        assert _send(at, method, path).status_code != 403, f"{method} {path} refused {minimum}"
    if minimum == Role.PENDING:
        return
    below = BELOW[minimum]
    with logged_in_client(app, below, follow_redirects=False) as c:
        status = _send(c, method, path).status_code
    if below == Role.PENDING:  # the guard keeps pending users on /pending
        assert status in (302, 303, 403), f"{method} {path} gave {status} to a pending user"
    else:
        assert status == 403, f"{method} {path} gave {status} to {below}"


def test_changes_view_refused_with_html_page_for_operator(app):
    with logged_in_client(app, Role.OPERATOR) as c:
        r = c.get("/history?view=changes", headers={"accept": "text/html"})
    assert r.status_code == 403 and "permission" in r.text


def _html(app, role, path="/"):
    with logged_in_client(app, role) as c:
        r = c.get(path)
    assert r.status_code == 200
    return r.text


def test_viewer_sees_no_controls(app):
    html = _html(app, Role.VIEWER)
    assert "/update" not in html.replace("/version-preview", "")
    for gone in ('action="/check"', 'action="/mailcow/update"', 'action="/self/update"',
                 "version-preview", 'href="/settings"', 'href="/users"', "Send digest now"):
        assert gone not in html
    assert 'href="/account"' in html and 'action="/auth/logout"' in html


def test_viewer_history_has_no_log_links_or_changes_tab(app):
    store = app.state.store
    job_id = store.create_job("build", "web", "someone@example.com")
    html = _html(app, Role.VIEWER, "/history")
    assert f"/jobs/{job_id}" not in html and "View log" not in html
    assert "view=changes" not in html


def test_operator_sees_updates_and_logs_but_not_admin_things(app):
    store = app.state.store
    job_id = store.create_job("build", "web", "someone@example.com")
    html = _html(app, Role.OPERATOR)
    assert 'action="/check"' in html and "/containers/" in html and "/update" in html
    assert 'action="/mailcow/update"' not in html and 'action="/self/update"' not in html
    assert 'href="/settings"' not in html and 'href="/users"' not in html
    hist = _html(app, Role.OPERATOR, "/history")
    assert f"/jobs/{job_id}" in hist and "view=changes" not in hist


def test_admin_sees_everything(app):
    html = _html(app, Role.ADMIN)
    for present in ('action="/check"', 'action="/mailcow/update"', 'action="/self/update"',
                    'href="/settings"', 'href="/users"', 'href="/account"'):
        assert present in html
    assert "view=changes" in _html(app, Role.ADMIN, "/history")
