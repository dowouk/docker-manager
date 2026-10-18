"""First start, setup token, the first Admin, and the 0.1/0.2 upgrade migration."""
import logging
import os
import re
import stat
import threading
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.entra import EntraIdentity
from app.main import create_app
from app.roles import Role
from app.store import Store
from auth_helpers import CSRF, assert_all_routes_guarded, sign_in

pytestmark = pytest.mark.fresh_install

HTML = {"Accept": "text/html"}
PASSWORD = "correct horse battery"
GROUP = "11111111-1111-1111-1111-111111111111"


def build(settings, tmp_path, **kw):
    return create_app(settings, docker_client=object(), http=httpx.AsyncClient(),
                      store=Store(str(tmp_path / "dashboard.db")), start_scheduler=False, **kw)


@pytest.fixture
def app(settings, tmp_path):
    app = build(settings, tmp_path)
    yield app
    app.state.store.close()


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


def token_of(tmp_path):
    return (tmp_path / "setup-token").read_text().strip()


def csrf_of(client, path="/setup"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def enter_token(client, tmp_path, token=None):
    csrf = csrf_of(client)
    return client.post("/setup/token", data={
        "token": token or token_of(tmp_path), "csrf_token": csrf})


def create_admin(client, **over):
    csrf = csrf_of(client, "/setup/admin")
    data = {"name": "Owner", "email": "owner@example.com", "password": PASSWORD,
            "confirm_password": PASSWORD, "csrf_token": csrf, **over}
    return client.post("/setup/admin", data=data)


def no_azure(monkeypatch):
    for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)


# ---- fresh install ----

def test_fresh_install_makes_token_file_and_logs_it_once(settings, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="app.setup"):
        app = build(settings, tmp_path)
    try:
        path = tmp_path / "setup-token"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        token = path.read_text().strip()
        assert len(token) >= 43  # 32 bytes, URL-safe
        lines = [r.getMessage() for r in caplog.records if token in r.getMessage()]
        assert len(lines) == 1 and "https://dash.example.com/setup" in lines[0]
        assert app.state.setup.is_open()
    finally:
        app.state.store.close()


def test_token_file_is_reused_on_restart(settings, tmp_path):
    first = build(settings, tmp_path)
    token = token_of(tmp_path)
    first.state.store.close()
    second = build(settings, tmp_path)
    second.state.store.close()
    assert token_of(tmp_path) == token


def test_token_never_stored_in_database(app, tmp_path):
    token = token_of(tmp_path)
    store = app.state.store
    assert token not in repr(store.load_settings())
    assert all(token not in e.summary for e in store.list_audit())
    assert all(token not in (store.get_meta(k) or "") for k in
               ("existing_install", "setup_complete", "allow_rules_migrated"))


def test_every_page_redirects_to_setup(client):
    for path in ("/", "/history", "/settings"):
        r = client.get(path, headers=HTML)
        assert r.status_code == 302 and r.headers["location"] == "/setup", path
    assert client.get("/healthz").status_code == 200
    assert client.get("/", headers={"Accept": "application/json"}).status_code == 401


def test_routes_still_guarded_with_setup_open(app):
    assert app.state.setup.is_open()
    assert_all_routes_guarded(app)


def test_wrong_token_refused_and_rate_limited(client, tmp_path):
    for _ in range(20):
        r = enter_token(client, tmp_path, "nope")
        assert r.status_code == 401
    r = enter_token(client, tmp_path, "nope")
    assert r.status_code == 429
    # even the right token is refused while blocked
    assert enter_token(client, tmp_path).status_code == 429
    assert client.get("/setup/admin").status_code == 303


def test_admin_form_needs_the_token_first(client, tmp_path):
    r = client.get("/setup/admin")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = client.post("/setup/admin", data={
        "email": "x@example.com", "password": PASSWORD, "confirm_password": PASSWORD,
        "csrf_token": csrf_of(client)})
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = enter_token(client, tmp_path)
    assert r.status_code == 303 and r.headers["location"] == "/setup/admin"
    assert client.get("/setup/admin").status_code == 200


def test_token_post_needs_csrf(client, tmp_path):
    client.get("/setup")
    r = client.post("/setup/token", data={"token": token_of(tmp_path)})
    assert r.status_code == 403


def test_creating_the_admin_closes_setup(app, client, tmp_path):
    token = token_of(tmp_path)
    enter_token(client, tmp_path)
    r = create_admin(client)
    assert r.status_code == 303 and r.headers["location"] == "/account/two-step"
    users = app.state.users.list()
    assert len(users) == 1 and users[0].role == "admin" and users[0].kind == "local"
    assert users[0].role_pinned and not users[0].must_change_password
    assert not (tmp_path / "setup-token").exists()
    assert app.state.store.get_meta("setup_complete") == "1"
    assert not app.state.setup.is_open()
    assert not app.state.setup.check_token(token, "1.2.3.4")
    assert getattr(app.state, "entra_setup_hook", None) is None
    assert any("First Admin created: owner@example.com" in e.summary
               for e in app.state.store.list_audit())
    assert token not in " ".join(e.summary for e in app.state.store.list_audit())
    # an anonymous visitor is back to the sign-in page, and /setup is gone for everyone
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as other:
        assert other.get("/setup", headers=HTML).headers["location"] == "/auth/login"
        assert other.post("/setup/token", data={"token": token}).status_code == 401
        sign_in(other, app)
        assert other.get("/setup").status_code == 404
        assert other.get("/setup/admin").status_code == 404


def test_new_admin_is_signed_in_and_must_enrol(client, tmp_path):
    enter_token(client, tmp_path)
    create_admin(client)
    r = client.get("/", headers=HTML)
    assert r.status_code == 302 and r.headers["location"] == "/account/two-step"


def test_weak_or_mismatched_password_rejected(app, client, tmp_path):
    enter_token(client, tmp_path)
    assert create_admin(client, password="short", confirm_password="short").status_code == 400
    assert create_admin(client, confirm_password="different one here").status_code == 400
    assert create_admin(client, email="not-an-email").status_code == 400
    assert app.state.users.count() == 0 and app.state.setup.is_open()


def test_concurrent_complete_local_makes_one_admin(app):
    results = []

    def go(n):
        results.append(app.state.setup.complete_local(f"N{n}", f"a{n}@example.com", PASSWORD))

    threads = [threading.Thread(target=go, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r is not None for r in results) == 1
    assert app.state.users.count() == 1


def test_loser_of_the_race_sees_a_clear_message(app, client, tmp_path, monkeypatch):
    enter_token(client, tmp_path)
    csrf = csrf_of(client, "/setup/admin")
    # the other request wins between this one's checks and its insert
    monkeypatch.setattr(app.state.users, "create_first_admin", lambda *a, **k: None)
    r = client.post("/setup/admin", data={
        "name": "Me", "email": "me@example.com", "password": PASSWORD,
        "confirm_password": PASSWORD, "csrf_token": csrf})
    assert r.status_code == 409 and "Setup has already been completed" in r.text
    assert "no access" not in r.text.lower()
    assert app.state.users.count() == 0


def test_public_url_saved_from_setup_request_when_unset(monkeypatch, tmp_path, settings):
    no_azure(monkeypatch)
    app = build(Settings(_env_file=None), tmp_path)
    try:
        with TestClient(app, base_url="https://dash.example.org", follow_redirects=False) as c:
            enter_token(c, tmp_path)
            create_admin(c)
        assert app.state.store.load_settings()["public_url"] == "https://dash.example.org"
        assert app.state.settings.public_url == "https://dash.example.org"
    finally:
        app.state.store.close()


def origin_for(scheme, host):
    from starlette.requests import Request

    from app.setup import _request_origin

    return _request_origin(Request({
        "type": "http", "scheme": scheme, "method": "GET", "path": "/setup/admin",
        "query_string": b"", "headers": [(b"host", host.encode())],
        "server": ("x", 80)}))


@pytest.mark.parametrize("scheme,host,expected", [
    ("https", "dash.example.org", "https://dash.example.org"),
    ("https", "dash.example.org:8443", "https://dash.example.org:8443"),
    ("http", "dash.example.org", None),  # plain http only for localhost
    ("http", "localhost:8080", "http://localhost:8080"),
    ("http", "127.0.0.1:8080", "http://127.0.0.1:8080"),
])
def test_public_url_from_request_needs_https_or_localhost(scheme, host, expected):
    assert origin_for(scheme, host) == expected


def test_existing_public_url_is_kept(app, client, tmp_path):
    enter_token(client, tmp_path)
    create_admin(client)
    assert "public_url" not in app.state.store.load_settings()
    assert app.state.settings.public_url == "https://dash.example.com"


# ---- Entra on a fresh install ----

def identity(**over):
    base = dict(tid="tenant", oid="oid-owner", upn="owner@example.com", name="Owner",
                roles=[], groups=None)
    return EntraIdentity(**{**base, **over})


def test_entra_setup_still_needs_the_token(app, client, tmp_path):
    r = client.post("/setup/entra", data={"csrf_token": csrf_of(client)})
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    enter_token(client, tmp_path)
    page = client.get("/setup/admin")
    assert "Sign in with Microsoft" in page.text
    r = client.post("/setup/entra", data={"csrf_token": csrf_of(client, "/setup/admin")})
    assert r.status_code == 303 and r.headers["location"] == "/auth/entra"


def test_microsoft_user_becomes_the_first_admin(app, client, tmp_path, settings):
    enter_token(client, tmp_path)
    client.post("/setup/entra", data={"csrf_token": csrf_of(client, "/setup/admin")})
    app.state.oauth.entra.authorize_access_token = AsyncMock(return_value={"userinfo": {
        "tid": "tenant", "iss": "https://login.microsoftonline.com/tenant/v2.0",
        "aud": "client", "oid": "oid-owner", "preferred_username": "owner@example.com",
        "name": "Owner"}})
    r = client.get("/auth/callback?code=1&state=2", headers=HTML)
    assert r.status_code == 303
    rec = app.state.users.by_email("entra", "owner@example.com")
    assert rec.role == "admin" and rec.role_pinned and rec.entra_oid == "oid-owner"
    assert app.state.users.count() == 1
    assert not (tmp_path / "setup-token").exists()
    assert app.state.entra_setup_hook is None
    assert not app.state.setup.is_open()
    assert client.get("/", headers=HTML).status_code == 200


def test_microsoft_setup_loser_sees_setup_completed(app, client, tmp_path, settings):
    enter_token(client, tmp_path)
    client.post("/setup/entra", data={"csrf_token": csrf_of(client, "/setup/admin")})
    assert app.state.setup.complete_local("Other", "other@example.com", PASSWORD)
    app.state.oauth.entra.authorize_access_token = AsyncMock(return_value={"userinfo": {
        "tid": "tenant", "iss": "https://login.microsoftonline.com/tenant/v2.0",
        "aud": "client", "oid": "oid-x", "preferred_username": "x@example.com", "name": "X"}})
    r = client.get("/auth/callback?code=1&state=2", headers=HTML)
    assert r.status_code == 409 and "Setup has already been completed" in r.text
    assert app.state.users.count() == 1


def test_entra_hook_refuses_once_closed(app):
    assert app.state.setup.complete_local("Other", "other@example.com", PASSWORD)
    assert app.state.setup.complete_with_entra(identity()) is None
    assert app.state.users.count() == 1


def test_entra_hook_is_installed_only_while_open(settings, tmp_path):
    app = build(settings, tmp_path)
    try:
        assert callable(app.state.entra_setup_hook)
    finally:
        app.state.store.close()


# ---- INITIAL_ADMIN_* ----

def initial_env(monkeypatch, tmp_path, password=PASSWORD + "\n"):
    f = tmp_path / "pw"
    f.write_text(password)
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "boss@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD_FILE", str(f))


def test_initial_admin_created_with_forced_password_change(monkeypatch, tmp_path, settings, caplog):
    initial_env(monkeypatch, tmp_path)
    app = build(settings, tmp_path)
    try:
        rec = app.state.users.by_email("local", "boss@example.com")
        assert rec.role == "admin" and rec.must_change_password
        assert not (tmp_path / "setup-token").exists()
        assert not app.state.setup.is_open()
        assert PASSWORD not in caplog.text
        from app.passwords import verify_password
        assert verify_password(rec.password_hash, PASSWORD)
        assert app.state.entra_setup_hook is None
    finally:
        app.state.store.close()


def test_initial_admin_without_public_url_falls_back_to_token(monkeypatch, tmp_path, settings, caplog):
    initial_env(monkeypatch, tmp_path)
    monkeypatch.delenv("PUBLIC_URL")
    with caplog.at_level(logging.ERROR, logger="app.setup"):
        app = build(Settings(_env_file=None), tmp_path)
    try:
        assert app.state.users.count() == 0
        assert (tmp_path / "setup-token").exists() and app.state.setup.is_open()
        assert "PUBLIC_URL" in caplog.text
    finally:
        app.state.store.close()


def test_initial_admin_weak_password_falls_back(monkeypatch, tmp_path, settings, caplog):
    initial_env(monkeypatch, tmp_path, password="short\n")
    app = build(settings, tmp_path)
    try:
        assert app.state.users.count() == 0 and app.state.setup.is_open()
        assert "short" not in caplog.text.replace("shortly", "")
    finally:
        app.state.store.close()


def test_initial_admin_ignored_once_a_user_exists(monkeypatch, tmp_path, settings):
    first = build(settings, tmp_path)
    first.state.setup.complete_local("Owner", "owner@example.com", PASSWORD)
    first.state.store.close()
    initial_env(monkeypatch, tmp_path)
    again = build(settings, tmp_path)
    try:
        assert again.state.users.count() == 1
        assert again.state.users.by_email("local", "boss@example.com") is None
    finally:
        again.state.store.close()


# ---- upgrading ----

def old_db(tmp_path):
    store = Store(str(tmp_path / "dashboard.db"))
    store.set_meta("existing_install", "1")
    store.close()


def test_upgrade_with_allowed_emails_pre_creates_admins(monkeypatch, tmp_path, settings):
    old_db(tmp_path)
    monkeypatch.setenv("ALLOWED_EMAILS", "a@x.com, B@x.com")
    s = Settings(_env_file=None)
    app = build(s, tmp_path)
    try:
        assert not (tmp_path / "setup-token").exists() and not app.state.setup.is_open()
        for email in ("a@x.com", "b@x.com"):
            rec = app.state.users.by_email("entra", email)
            assert rec.role == "admin" and rec.role_pinned and rec.entra_oid is None
        assert app.state.settings.default_role == "pending"  # never admin_legacy
        assert app.state.store.get_meta("allow_rules_migrated") == "1"
        assert app.state.entra_setup_hook is None
        audit = [e.summary for e in app.state.store.list_audit()
                 if e.summary.startswith("Upgraded from an earlier version")]
        assert len(audit) == 1 and "a@x.com" in audit[0]
    finally:
        app.state.store.close()


def test_migration_runs_once(monkeypatch, tmp_path):
    old_db(tmp_path)
    monkeypatch.setenv("ALLOWED_EMAILS", "a@x.com")
    first = build(Settings(_env_file=None), tmp_path)
    rec = first.state.users.by_email("entra", "a@x.com")
    first.state.users.create("local", "keep@x.com", "K", "admin")
    first.state.users.delete(rec.id)
    first.state.store.close()
    second = build(Settings(_env_file=None), tmp_path)
    try:
        assert second.state.users.by_email("entra", "a@x.com") is None
        assert sum(e.summary.startswith("Upgraded") for e in second.state.store.list_audit()) == 1
    finally:
        second.state.store.close()


def test_upgrade_with_group_maps_it_to_admin(monkeypatch, tmp_path):
    old_db(tmp_path)
    monkeypatch.setenv("ALLOWED_GROUP_ID", GROUP)
    app = build(Settings(_env_file=None), tmp_path)
    try:
        assert app.state.settings_service.group_mappings() == [
            {"group_id": GROUP, "role": "admin"}]
        assert app.state.settings.default_role == "pending"
        assert app.state.users.count() == 0 and not app.state.setup.is_open()
    finally:
        app.state.store.close()


def test_upgrade_with_no_rules_uses_admin_legacy_and_banner(tmp_path, settings):
    old_db(tmp_path)
    app = build(settings, tmp_path)
    try:
        assert app.state.settings.default_role == "admin_legacy"
        assert not (tmp_path / "setup-token").exists() and not app.state.setup.is_open()
        banner = "Everyone who can sign in is an Admin, as in 0.1"
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
            sign_in(c, app, Role.ADMIN)
            assert banner in c.get("/", headers=HTML).text
            assert banner in c.get("/history", headers=HTML).text
            app.state.settings_service.save(
                "signin", {"local_sign_in": "on", "default_role": "viewer"}, "admin@x")
            from app.main import apply_settings
            apply_settings(app)
            assert banner not in c.get("/", headers=HTML).text
    finally:
        app.state.store.close()


def test_banner_hidden_from_non_admins(tmp_path, settings):
    old_db(tmp_path)
    app = build(settings, tmp_path)
    try:
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
            sign_in(c, app, Role.OPERATOR)
            assert "as in 0.1" not in c.get("/", headers=HTML).text
    finally:
        app.state.store.close()


def test_migration_happens_before_first_request(monkeypatch, tmp_path):
    old_db(tmp_path)
    monkeypatch.setenv("ALLOWED_EMAILS", "a@x.com")
    app = build(Settings(_env_file=None), tmp_path)  # no TestClient started yet
    try:
        assert app.state.users.by_email("entra", "a@x.com") is not None
    finally:
        app.state.store.close()


# ---- review fixes ----

def good_claims(**over):
    return {"userinfo": {
        "tid": "tenant", "iss": "https://login.microsoftonline.com/tenant/v2.0",
        "aud": "client", "oid": "oid-x", "preferred_username": "x@example.com",
        "name": "X", **over}}


def test_direct_entra_login_while_setup_open_goes_to_setup(client):
    r = client.get("/auth/entra", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/setup"


@pytest.mark.parametrize("extra", [{}, {"roles": ["DockerManager.Admin"]}])
def test_callback_without_setup_flag_creates_nobody(app, client, tmp_path, extra):
    app.state.oauth.entra.authorize_access_token = AsyncMock(
        return_value=good_claims(**extra))
    r = client.get("/auth/callback?code=1&state=2", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert app.state.users.count() == 0 and app.state.setup.is_open()
    assert (tmp_path / "setup-token").exists()


def test_sign_in_entra_refuses_while_setup_open(app, settings):
    from app.entra import sign_in_entra

    assert sign_in_entra(app.state.users, identity(), settings, [], setup_open=True) is None
    assert app.state.users.count() == 0


def test_token_step_rotates_the_session(client, tmp_path):
    client.get("/setup")
    before = client.cookies.get("session")
    old_csrf = csrf_of(client)
    r = enter_token(client, tmp_path)
    assert r.status_code == 303
    after = client.cookies.get("session")
    assert after and after != before
    assert csrf_of(client, "/setup/admin") != old_csrf
    assert client.get("/setup/admin").status_code == 200


def test_oversized_initial_password_file_refused(monkeypatch, tmp_path, settings, caplog):
    initial_env(monkeypatch, tmp_path, password=("x" * 5000))
    with caplog.at_level(logging.ERROR, logger="app.setup"):
        app = build(settings, tmp_path)
    try:
        assert app.state.users.count() == 0 and app.state.setup.is_open()
        assert "xxxx" not in caplog.text and "larger than" in caplog.text
    finally:
        app.state.store.close()


def test_token_file_removed_whenever_setup_closes(app, tmp_path):
    assert (tmp_path / "setup-token").exists()
    app.state.users.create("local", "x@example.com", "X", "admin")  # another path
    assert not app.state.setup.is_open()
    assert not (tmp_path / "setup-token").exists()


def test_bad_token_file_is_regenerated_and_fixed_to_0600(settings, tmp_path):
    first = build(settings, tmp_path)
    first.state.store.close()
    path = tmp_path / "setup-token"
    path.write_text("short\n")
    second = build(settings, tmp_path)
    second.state.store.close()
    assert path.read_text().strip() != "short" and len(path.read_text().strip()) == 43
    good = path.read_text()
    os.chmod(path, 0o644)
    third = build(settings, tmp_path)
    third.state.store.close()
    assert path.read_text() == good and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_non_guid_group_is_reported_not_silently_dropped(monkeypatch, tmp_path, caplog):
    old_db(tmp_path)
    monkeypatch.setenv("ALLOWED_GROUP_ID", "not-a-guid")
    with caplog.at_level(logging.ERROR, logger="app.setup"):
        app = build(Settings(_env_file=None), tmp_path)
    try:
        assert app.state.settings_service.group_mappings() == []
        assert "reset-admin" in caplog.text
        audit = [e.summary for e in app.state.store.list_audit()
                 if e.summary.startswith("Upgraded")][0]
        assert "group was skipped" in audit
        assert app.state.settings.default_role == "pending"
    finally:
        app.state.store.close()
