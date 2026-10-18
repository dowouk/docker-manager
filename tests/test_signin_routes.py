"""Local sign-in, the two-step step, forced password change and TOTP enrolment."""
import re
from datetime import datetime, timedelta, timezone

import httpx
import pyotp
import pytest
import respx
from fastapi.testclient import TestClient

from app import totp
from app.main import create_app
from app.passwords import hash_password
from app.roles import Role
from app.store import Store
from auth_helpers import CSRF, assert_all_routes_guarded, set_session

PASSWORD = "correct horse battery"
SECRET = "JBSWY3DPEHPK3PXP"
HTML = {"Accept": "text/html"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(settings, tmp_path, clock):
    app = create_app(
        settings, docker_client=object(), http=httpx.AsyncClient(),
        store=Store(str(tmp_path / "data" / "dashboard.db")), start_scheduler=False,
    )
    app.state.users._clock = clock
    yield app
    app.state.store.close()


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


def set_flags(app, **kw):
    app.state.settings = app.state.settings.model_copy(update=kw)


def add_user(app, email="ann@example.com", role=Role.VIEWER, totp_on=False, **kw):
    users = app.state.users
    rec = users.create("local", email, "Ann", role.value,
                       password_hash=hash_password(kw.pop("password", PASSWORD)), **kw)
    if totp_on:
        users.set_totp(rec.id, SECRET, True)
    return users.get(rec.id)


def csrf_of(client, path="/auth/login"):
    r = client.get(path)
    return re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)


def login(client, email="ann@example.com", password=PASSWORD):
    token = csrf_of(client)
    return client.post("/auth/login", data={"csrf_token": token, "email": email,
                                            "password": password})


def audit(app):
    return [(e.kind, e.summary) for e in app.state.store.list_audit()]


def strip_csrf(text):
    return re.sub(r'(name="csrf_token" value=")[^"]+', r"\1X", text)


# --- password step ---------------------------------------------------------------

def test_correct_password_signs_in_and_rotates_cookie(app, client):
    add_user(app)
    client.get("/auth/login")
    anon = client.cookies.get("session")
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.cookies.get("session") != anon
    assert client.get("/", headers=HTML).status_code != 302
    assert ("signin", "Signed in (local)") in audit(app)
    assert app.state.users.by_email("local", "ann@example.com").last_sign_in is not None


def test_anonymous_session_cannot_be_reused_after_sign_in(app, client):
    add_user(app)
    client.get("/auth/login")
    anon = client.cookies.get("session")
    login(client)
    other = TestClient(app, base_url="https://testserver", follow_redirects=False)
    other.cookies.set("session", anon, domain="testserver.local", path="/")
    assert other.get("/", headers=HTML).status_code == 302


def test_wrong_password_and_unknown_email_are_identical(app, client):
    add_user(app)
    a = login(client, password="nope nope nope")
    b = login(client, email="ghost@example.com", password="nope nope nope")
    assert a.status_code == b.status_code == 401
    assert "Email or password is wrong" in a.text
    assert strip_csrf(a.text) == strip_csrf(b.text)


def test_disabled_user_looks_like_wrong_password(app, client):
    rec = add_user(app)
    app.state.users.update(rec.id, disabled=True)
    r = login(client)
    assert r.status_code == 401 and "Email or password is wrong" in r.text


def test_failure_is_audited_and_counts_for_unknown_emails(app, client):
    login(client, email="ghost@example.com", password="x")
    assert ("signin", "Sign-in failed: ghost@example.com") in audit(app)
    assert app.state.users.failures("account", "ghost@example.com", timedelta(minutes=15)) == 1


def test_sign_in_requires_csrf(app, client):
    add_user(app)
    client.get("/auth/login")
    r = client.post("/auth/login", data={"email": "ann@example.com", "password": PASSWORD})
    assert r.status_code == 403


def test_account_lock_after_five_failures_and_expiry(app, client, clock):
    add_user(app)
    for _ in range(5):
        assert login(client, password="wrong wrong wrong").status_code == 401
    r = login(client)  # correct, but locked
    assert r.status_code == 429 and "Too many attempts – try again in 15 minutes." in r.text
    assert "location" not in r.headers
    assert any("locked" in s.lower() for k, s in audit(app))
    # the same message for an email that doesn't exist, once it is locked too
    for _ in range(5):
        login(client, email="ghost@example.com", password="x")
    g = login(client, email="ghost@example.com", password="x")
    assert g.status_code == 429 and "Too many attempts" in g.text
    clock.advance(minutes=16)
    assert login(client).status_code == 303


def test_ip_blocked_after_twenty_failures_across_emails(app, client, clock):
    add_user(app)
    for i in range(20):
        login(client, email=f"user{i}@example.com", password="x")
    r = login(client)
    assert r.status_code == 429
    clock.advance(minutes=16)
    assert login(client).status_code == 303


def test_successful_sign_in_clears_that_accounts_failures(app, client):
    add_user(app)
    for _ in range(3):
        login(client, password="wrong wrong wrong")
    assert login(client).status_code == 303
    assert app.state.users.failures("account", "ann@example.com", timedelta(minutes=15)) == 0


def test_email_is_case_insensitive(app, client):
    add_user(app)
    assert login(client, email="ANN@Example.com").status_code == 303


# --- the page ----------------------------------------------------------------------

def test_page_shows_both_methods_by_default(app, client):
    r = client.get("/auth/login")
    assert "Sign in with Microsoft" in r.text and 'href="/auth/entra"' in r.text
    assert 'name="email"' in r.text and "Admin sign-in" not in r.text


def test_local_form_hidden_when_local_sign_in_off(app, client):
    set_flags(app, local_sign_in=False)
    r = client.get("/auth/login")
    assert 'name="email"' not in r.text and "Sign in with Microsoft" in r.text
    add_user(app)
    set_session(client, app, {"csrf": CSRF})
    r = client.post("/auth/login", data={"csrf_token": CSRF, "email": "ann@example.com",
                                         "password": PASSWORD})
    assert r.status_code == 403 and "location" not in r.headers


def test_admin_sign_in_disclosure(app, client):
    set_flags(app, hide_local_for_non_admins=True)
    r = client.get("/auth/login")
    assert "Admin sign-in" in r.text and "<details" in r.text and 'name="email"' in r.text


def test_nothing_available_message(app, client, monkeypatch):
    set_flags(app, local_sign_in=False, azure_client_secret=None)
    r = client.get("/auth/login")
    assert "Sign-in isn't set up – see the README (reset-admin)" in r.text


def test_entra_off_hides_microsoft_button(app, client):
    set_flags(app, azure_client_secret=None)
    r = client.get("/auth/login")
    assert "Sign in with Microsoft" not in r.text and 'name="email"' in r.text


@respx.mock
def test_entra_redirect_moved_to_entra_route(client):
    from test_entra import METADATA
    respx.get("https://login.microsoftonline.com/tenant/v2.0/.well-known/openid-configuration"
              ).mock(return_value=httpx.Response(200, json=METADATA))
    r = client.get("/auth/entra")
    assert r.status_code == 302 and r.headers["location"].startswith(
        METADATA["authorization_endpoint"])


# --- two-step step -----------------------------------------------------------------

def code_now():
    return pyotp.TOTP(SECRET).now()


def test_totp_required_then_signs_in(app, client):
    add_user(app, totp_on=True)
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/auth/totp"
    assert client.get("/", headers=HTML).status_code == 302  # not signed in yet
    token = csrf_of(client, "/auth/totp")
    r = client.post("/auth/totp", data={"csrf_token": token, "code": code_now()})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/", headers=HTML).status_code != 302


def test_wrong_code_refused_and_counted(app, client):
    add_user(app, totp_on=True)
    login(client)
    token = csrf_of(client, "/auth/totp")
    r = client.post("/auth/totp", data={"csrf_token": token, "code": "000000"})
    assert r.status_code == 401
    assert app.state.users.failures("account", "ann@example.com", timedelta(minutes=15)) == 1


def test_replayed_code_refused(app, client):
    add_user(app, totp_on=True)
    code = code_now()
    login(client)
    token = csrf_of(client, "/auth/totp")
    assert client.post("/auth/totp", data={"csrf_token": token, "code": code}).status_code == 303
    client.cookies.clear()
    login(client)
    token = csrf_of(client, "/auth/totp")
    r = client.post("/auth/totp", data={"csrf_token": token, "code": code})
    assert r.status_code == 401


def test_recovery_code_works_once(app, client):
    rec = add_user(app, totp_on=True)
    codes = totp.new_recovery_codes()
    app.state.users.set_recovery_codes(rec.id, [totp.hash_recovery_code(c) for c in codes])
    login(client)
    token = csrf_of(client, "/auth/totp")
    assert client.post("/auth/totp", data={"csrf_token": token, "code": codes[0]}).status_code == 303
    assert ("signin", "Recovery code used") in audit(app)
    assert len(app.state.users.get(rec.id).recovery_codes) == 9
    client.cookies.clear()
    login(client)
    token = csrf_of(client, "/auth/totp")
    assert client.post("/auth/totp", data={"csrf_token": token, "code": codes[0]}).status_code == 401


def test_pending_state_expires_after_five_minutes(app, client, clock):
    add_user(app, totp_on=True)
    login(client)
    token = csrf_of(client, "/auth/totp")
    clock.advance(minutes=5, seconds=1)
    r = client.post("/auth/totp", data={"csrf_token": token, "code": code_now()})
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    assert client.get("/", headers=HTML).status_code == 302


def test_totp_page_without_pending_goes_to_login(client):
    r = client.get("/auth/totp")
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"


def test_totp_failures_lock_the_account(app, client):
    add_user(app, totp_on=True)
    login(client)
    token = csrf_of(client, "/auth/totp")
    for _ in range(5):
        client.post("/auth/totp", data={"csrf_token": token, "code": "000000"})
    r = client.post("/auth/totp", data={"csrf_token": token, "code": code_now()})
    assert r.status_code == 429
    assert login(client).status_code == 429


# --- forced flows ------------------------------------------------------------------

def test_must_change_password_is_forced(app, client):
    add_user(app, must_change_password=True)
    r = login(client)
    assert r.headers["location"] == "/account/password"
    r = client.get("/", headers=HTML)
    assert r.status_code == 302 and r.headers["location"] == "/account/password"
    assert client.get("/history", headers=HTML).headers["location"] == "/account/password"
    assert client.post("/settings/checking", data={}).status_code == 403
    assert client.get("/account/password").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_forced_password_change_clears_flag_and_ends_other_sessions(app, client):
    rec = add_user(app, must_change_password=True)
    app.state.sessions.create({"user_id": rec.id, "csrf": "x"})  # another device
    login(client)
    token = csrf_of(client, "/account/password")
    r = client.post("/account/password", data={
        "csrf_token": token, "new_password": "a brand new passphrase",
        "confirm_password": "a brand new passphrase"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    fresh = app.state.users.get(rec.id)
    assert not fresh.must_change_password
    assert ("account", "Password changed") in audit(app)
    assert client.get("/", headers=HTML).status_code != 302  # this session survives
    with app.state.store._lock:
        n = app.state.store._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (rec.id,)).fetchone()[0]
    assert n == 1


def test_password_change_refuses_common_short_and_mismatch(app, client):
    add_user(app, must_change_password=True)
    login(client)
    token = csrf_of(client, "/account/password")
    for pw, confirm, text in [("masterbating", "masterbating", "too common"),
                              ("short", "short", "at least 12"),
                              ("a brand new passphrase", "different one here", "two passwords don&#39;t match")]:
        r = client.post("/account/password", data={
            "csrf_token": token, "new_password": pw, "confirm_password": confirm})
        assert r.status_code == 400 and text in r.text


def test_voluntary_change_needs_current_password(app, client):
    add_user(app)
    login(client)
    token = csrf_of(client, "/account/password")
    data = {"csrf_token": token, "new_password": "a brand new passphrase",
            "confirm_password": "a brand new passphrase"}
    r = client.post("/account/password", data={**data, "current_password": "wrong"})
    assert r.status_code == 400 and "current password" in r.text
    r = client.post("/account/password", data={**data, "current_password": PASSWORD})
    assert r.status_code == 200 and "Password changed" in r.text
    client.cookies.clear()
    assert login(client, password="a brand new passphrase").status_code == 303


def test_local_admin_without_totp_is_forced_to_enrol(app, client):
    add_user(app, role=Role.ADMIN)
    r = login(client)
    assert r.headers["location"] == "/account/two-step"
    r = client.get("/", headers=HTML)
    assert r.headers["location"] == "/account/two-step"
    assert client.get("/account/two-step").status_code == 200


def test_enrolment_flow_shows_recovery_codes_once(app, client):
    rec = add_user(app, role=Role.ADMIN)
    login(client)
    page = client.get("/account/two-step")
    assert "<svg" in page.text
    secret = re.search(r'class="secret-key">([A-Z2-7]+)<', page.text).group(1)
    token = csrf_of(client, "/account/two-step")
    r = client.post("/account/two-step", data={"csrf_token": token, "code": "000000"})
    assert r.status_code == 400
    assert not app.state.users.get(rec.id).totp_enabled  # nothing stored unconfirmed
    assert app.state.users.get(rec.id).totp_secret is None
    r = client.post("/account/two-step", data={"csrf_token": token,
                                               "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200
    codes = re.findall(r"<code>([a-z0-9]{5}-[a-z0-9]{5})</code>", r.text)
    assert len(codes) == 10
    assert ("account", "Two-step sign-in turned on") in audit(app)
    assert secret not in str(audit(app)) and not any(c in str(audit(app)) for c in codes)
    stored = app.state.users.get(rec.id)
    assert stored.totp_enabled and len(stored.recovery_codes) == 10
    assert codes[0] not in stored.recovery_codes  # only hashes are stored
    assert client.get("/", headers=HTML).status_code != 302  # no longer forced
    again = client.get("/account/two-step")
    assert codes[0] not in again.text


def test_enrolment_not_forced_when_setting_off_or_entra_admin(app, client):
    set_flags(app, require_totp_admins=False)
    add_user(app, role=Role.ADMIN)
    assert login(client).headers["location"] == "/"
    set_flags(app, require_totp_admins=True)
    entra = app.state.users.create("entra", "boss@example.com", "Boss", "admin")
    set_session(client, app, {"user_id": entra.id, "csrf": CSRF})
    assert client.get("/", headers=HTML).status_code != 302


def test_non_admin_not_forced_to_enrol(app, client):
    add_user(app, role=Role.OPERATOR)
    assert login(client).headers["location"] == "/"


def test_forced_page_applies_on_live_record(app, client):
    rec = add_user(app)
    login(client)
    assert client.get("/", headers=HTML).status_code != 302
    app.state.users.set_password_hash(rec.id, hash_password(PASSWORD), True)
    assert client.get("/", headers=HTML).headers["location"] == "/account/password"


def test_all_routes_still_guarded(app):
    assert_all_routes_guarded(app)


# --- fix round 1 -------------------------------------------------------------------

def test_lock_lasts_fifteen_minutes_from_when_it_trips(app, client, clock):
    add_user(app)
    for _ in range(5):
        assert login(client, password="wrong wrong wrong").status_code == 401
        clock.advance(minutes=3, seconds=30)
    # 5th failure was at t=14 min; the first has aged out only at t=15
    clock.advance(minutes=-3, seconds=-30)  # back to t=14
    assert login(client).status_code == 429
    clock.advance(minutes=14)  # t=28
    assert login(client).status_code == 429
    clock.advance(minutes=1, seconds=1)  # t=29:01
    assert login(client).status_code == 303


def test_ip_block_audit_is_not_an_account_lock(app, client):
    for i in range(20):
        login(client, email=f"user{i}@example.com", password="x")
    kinds = audit(app)
    assert ("signin", "Sign-ins blocked from an address") in kinds
    assert not any(s.startswith("Sign-in locked") and "user19" in s for _, s in kinds)


def test_email_is_stripped_before_lookup(app, client):
    add_user(app)
    assert login(client, email="  ann@example.com ").status_code == 303


def test_unconfirmed_totp_secret_is_encrypted_in_the_session_row(app, client):
    add_user(app, role=Role.ADMIN)
    login(client)
    page = client.get("/account/two-step")
    secret = re.search(r'class="secret-key">([A-Z2-7]+)<', page.text).group(1)
    with app.state.store._lock:
        rows = app.state.store._db.execute("SELECT data FROM sessions").fetchall()
    assert rows and all(secret not in r[0] for r in rows)
    assert any("totp_setup_secret" in r[0] for r in rows)


def test_totp_step_is_compare_and_set(app, client, monkeypatch):
    rec = add_user(app, totp_on=True)
    users = app.state.users
    assert users.set_totp_last_step(rec.id, 100) is True
    assert users.set_totp_last_step(rec.id, 100) is False
    assert users.set_totp_last_step(rec.id, 99) is False
    assert users.set_totp_last_step(rec.id, 101) is True
    login(client)
    token = csrf_of(client, "/auth/totp")
    monkeypatch.setattr(users, "set_totp_last_step", lambda *a: False)  # lost the race
    r = client.post("/auth/totp", data={"csrf_token": token, "code": code_now()})
    assert r.status_code == 401


def test_voluntary_enrolment_asks_for_current_password_first(app, client):
    rec = add_user(app)
    login(client)
    page = client.get("/account/two-step")
    assert "current_password" in page.text and "secret-key" not in page.text
    token = csrf_of(client, "/account/two-step")
    r = client.post("/account/two-step", data={"csrf_token": token, "current_password": "wrong"})
    assert r.status_code == 400 and "secret-key" not in r.text
    r = client.post("/account/two-step", data={"csrf_token": token, "current_password": PASSWORD})
    assert r.status_code == 200 and "secret-key" in r.text
    secret = re.search(r'class="secret-key">([A-Z2-7]+)<', r.text).group(1)
    r = client.post("/account/two-step", data={"csrf_token": token, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200 and app.state.users.get(rec.id).totp_enabled


def test_posts_refuse_missing_csrf(app, client):
    add_user(app, totp_on=True)
    login(client)
    token = csrf_of(client, "/auth/totp")
    assert client.post("/auth/totp", data={"code": code_now()}).status_code == 403
    client.post("/auth/totp", data={"csrf_token": token, "code": code_now()})
    assert client.post("/account/password", data={"new_password": "x"}).status_code == 403
    assert client.post("/account/two-step", data={"current_password": PASSWORD}).status_code == 403


def test_voluntary_password_change_ends_other_sessions(app, client):
    rec = add_user(app)
    app.state.sessions.create({"user_id": rec.id, "csrf": "x"})
    login(client)
    token = csrf_of(client, "/account/password")
    r = client.post("/account/password", data={
        "csrf_token": token, "current_password": PASSWORD,
        "new_password": "a brand new passphrase", "confirm_password": "a brand new passphrase"})
    assert r.status_code == 200
    with app.state.store._lock:
        n = app.state.store._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (rec.id,)).fetchone()[0]
    assert n == 1
    assert client.get("/", headers=HTML).status_code != 302


def test_forced_user_is_sent_back_from_the_other_page(app, client):
    add_user(app, role=Role.ADMIN, must_change_password=True)
    login(client)
    assert client.get("/account/two-step", headers=HTML).headers["location"] == "/account/password"
    app.state.users.set_password_hash(
        app.state.users.by_email("local", "ann@example.com").id, hash_password(PASSWORD), False)
    assert client.get("/account/password", headers=HTML).headers["location"] == "/account/two-step"
