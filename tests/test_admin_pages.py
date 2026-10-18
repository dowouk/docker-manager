"""Users page, Account page and invites."""
import re
from datetime import datetime, timedelta, timezone

import httpx
import pyotp
import pytest
from fastapi.testclient import TestClient

from app import totp
from app.main import create_app
from app.passwords import hash_password, verify_password
from app.roles import Role
from app.store import Store
from auth_helpers import CSRF, assert_all_routes_guarded, logged_in_client, make_user, set_session

PASSWORD = "correct horse battery"
LAST_ADMIN = "There must always be at least one active Admin"
HTML = {"Accept": "text/html"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


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
def admin(app):
    with logged_in_client(app, Role.ADMIN, follow_redirects=False) as c:
        yield c


def post(client, path, **data):
    return client.post(path, data={"csrf_token": CSRF, **data})


def other_admin(app, email="boss@example.com"):
    return make_user(app, Role.ADMIN, email=email, name="Boss")


def local(app, email="ann@example.com", role=Role.VIEWER, **kw):
    return app.state.users.create("local", email, "Ann", role.value,
                                  password_hash=hash_password(PASSWORD), **kw)


def secret_after(html, marker):
    return re.search(marker + r'.*?<code class="secret-key">([^<]+)</code>', html, re.S).group(1)


def anonymous(app):
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def anon_csrf(client, path):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


# --- users page ---------------------------------------------------------------

def test_users_table_lists_pending_first_with_approve_buttons(app, admin):
    app.state.users.create("entra", "wait@example.com", "Wanda", "pending")
    local(app, "zed@example.com")
    html = admin.get("/users", headers=HTML).text
    assert html.index("wait@example.com") < html.index("zed@example.com")
    for role in ("Viewer", "Operator", "Admin"):
        assert f"Approve as {role}" in html
    assert "Waiting for approval" in html and "Local" in html and "Microsoft" in html


def test_approve_pins_role_and_lets_user_in(app, admin):
    pend = app.state.users.create("local", "wait@example.com", "Wanda", "pending")
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        set_session(c, app, {"user_id": pend.id, "csrf": CSRF})
        assert c.get("/", headers=HTML).status_code in (302, 403)
        assert post(admin, f"/users/{pend.id}/role", role="operator").status_code == 303
        rec = app.state.users.get(pend.id)
        assert rec.role == "operator" and rec.role_pinned
        assert c.get("/", headers=HTML).status_code == 200
    assert any(a.kind == "users" and "Approved wait@example.com" in a.summary
               for a in app.state.store.list_audit())


def test_change_role_pins_and_unpin_releases(app, admin):
    rec = app.state.users.create("entra", "e@example.com", "E", "viewer")
    assert post(admin, f"/users/{rec.id}/role", role="admin").status_code == 303
    assert app.state.users.get(rec.id).role_pinned
    assert post(admin, f"/users/{rec.id}/unpin").status_code == 303
    assert not app.state.users.get(rec.id).role_pinned


def test_unpin_then_entra_callback_recomputes_role(app, admin, settings):
    from unittest.mock import AsyncMock

    claims = {
        "tid": settings.azure_tenant_id,
        "iss": f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0",
        "aud": settings.azure_client_id, "oid": "oid-eve",
        "preferred_username": "eve@corp.example", "name": "Eve",
        "roles": ["DockerManager.Admin"],
    }

    def callback():
        app.state.oauth.entra.authorize_access_token = AsyncMock(
            return_value={"userinfo": claims})
        with anonymous(app) as c:
            assert c.get("/auth/callback?code=1&state=2", headers=HTML).status_code == 303

    rec = app.state.users.create("entra", "eve@corp.example", "Eve", "viewer", pinned=True)
    callback()
    assert app.state.users.get(rec.id).role == "viewer"  # pinned: the mapping is ignored
    assert post(admin, f"/users/{rec.id}/unpin").status_code == 303
    callback()
    after = app.state.users.get(rec.id)
    assert after.role == "admin" and not after.role_pinned


def test_unpin_refused_for_local_users(app, admin):
    ann = local(app)
    r = post(admin, f"/users/{ann.id}/unpin")
    assert r.status_code == 400 and "Only Microsoft users" in r.text


def test_disable_ends_live_session_immediately(app, admin):
    ann = local(app)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        set_session(c, app, {"user_id": ann.id, "csrf": CSRF})
        assert c.get("/", headers=HTML).status_code == 200
        assert post(admin, f"/users/{ann.id}/disable").status_code == 303
        assert c.get("/", headers=HTML).status_code == 302
    assert app.state.users.get(ann.id).disabled
    assert post(admin, f"/users/{ann.id}/enable").status_code == 303
    assert not app.state.users.get(ann.id).disabled


def test_sign_out_ends_sessions(app, admin):
    ann = local(app)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        set_session(c, app, {"user_id": ann.id, "csrf": CSRF})
        post(admin, f"/users/{ann.id}/sign-out")
        assert c.get("/", headers=HTML).status_code == 302


def test_last_admin_cannot_be_demoted_disabled_or_deleted(app, admin):
    me = app.state.users.by_email("local", "test@example.com")
    for path, data in ((f"/users/{me.id}/role", {"role": "viewer"}),
                       (f"/users/{me.id}/disable", {}),
                       (f"/users/{me.id}/delete", {"confirm_email": me.email})):
        r = post(admin, path, **data)
        assert r.status_code == 400 and LAST_ADMIN in r.text
    rec = app.state.users.get(me.id)
    assert rec.role == "admin" and not rec.disabled


def test_demoting_one_of_two_admins_works(app, admin):
    boss = other_admin(app)
    assert post(admin, f"/users/{boss.id}/role", role="viewer").status_code == 303


def test_one_time_password_works_once_and_forces_change(app, admin):
    r = post(admin, "/users/local", name="Ann", email="ann@example.com",
             role="operator", method="password")
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
    password = secret_after(r.text, "One-time password")
    assert len(password) == 16
    rec = app.state.users.by_email("local", "ann@example.com")
    assert rec.must_change_password and rec.role == "operator" and rec.role_pinned
    assert password not in "".join(a.summary for a in app.state.store.list_audit())
    assert password not in admin.get("/users").text  # shown once
    with anonymous(app) as c:
        token = anon_csrf(c, "/auth/login")
        ok = c.post("/auth/login", data={"csrf_token": token, "email": "ann@example.com",
                                         "password": password})
        assert ok.status_code == 303 and ok.headers["location"] == "/account/password"
        forced = c.get("/", headers=HTML)
        assert forced.status_code == 302 and forced.headers["location"] == "/account/password"
    with anonymous(app) as c:  # change it, then the old one-time password is dead
        set_session(c, app, {"user_id": rec.id, "csrf": CSRF})
        r = c.post("/account/password", data={
            "csrf_token": CSRF, "new_password": "a brand new passphrase",
            "confirm_password": "a brand new passphrase"})
        assert r.status_code == 303
    with anonymous(app) as c:
        token = anon_csrf(c, "/auth/login")
        bad = c.post("/auth/login", data={"csrf_token": token, "email": "ann@example.com",
                                          "password": password})
        assert bad.status_code == 401


def test_create_local_validation_and_duplicates(app, admin):
    assert post(admin, "/users/local", email="nope", role="viewer",
                method="invite").status_code == 400
    assert post(admin, "/users/local", email="a@example.com", role="pending",
                method="invite").status_code == 400
    assert post(admin, "/users/local", email="a@example.com", role="viewer",
                method="invite").status_code == 200
    r = post(admin, "/users/local", email="A@example.com", role="viewer", method="invite")
    assert r.status_code == 400 and "already exists" in r.text


def test_invite_link_shown_once_works_once(app, admin):
    r = post(admin, "/users/local", name="Ann", email="ann@example.com",
             role="viewer", method="invite")
    link = secret_after(r.text, "Invite link")
    assert link.startswith("https://dash.example.com/auth/invite/")
    path = link.removeprefix("https://dash.example.com")
    token = path.rsplit("/", 1)[1]
    assert token not in "".join(a.summary for a in app.state.store.list_audit())
    with anonymous(app) as c:
        page = c.get(path)
        assert page.status_code == 200 and "ann@example.com" in page.text
        assert page.headers["referrer-policy"] == "no-referrer"
        csrf = anon_csrf(c, path)
        weak = c.post(path, data={"csrf_token": csrf, "new_password": "short",
                                  "confirm_password": "short"})
        assert weak.status_code == 400  # a typo doesn't burn the invite
        good = c.post(path, data={"csrf_token": csrf, "name": "Ann B",
                                  "new_password": PASSWORD, "confirm_password": PASSWORD})
        assert good.status_code == 303 and good.headers["location"] == "/"
        assert c.get("/", headers=HTML).status_code == 200
    rec = app.state.users.by_email("local", "ann@example.com")
    assert rec.name == "Ann B" and verify_password(rec.password_hash, PASSWORD)
    assert not rec.must_change_password
    with anonymous(app) as c:
        assert c.get(path).status_code == 410
        csrf = anon_csrf(c, "/auth/login")
        assert c.post(path, data={"csrf_token": csrf, "new_password": PASSWORD,
                                  "confirm_password": PASSWORD}).status_code == 410


def test_invite_expires_after_72_hours(app, admin, clock):
    r = post(admin, "/users/local", email="ann@example.com", role="viewer", method="invite")
    path = secret_after(r.text, "Invite link").removeprefix("https://dash.example.com")
    clock.now += timedelta(hours=71)
    with anonymous(app) as c:
        assert c.get(path).status_code == 200
    clock.now += timedelta(hours=2)
    with anonymous(app) as c:
        assert c.get(path).status_code == 410


def test_invite_for_admin_forces_two_step_enrolment(app, admin):
    r = post(admin, "/users/local", email="root@example.com", role="admin", method="invite")
    path = secret_after(r.text, "Invite link").removeprefix("https://dash.example.com")
    with anonymous(app) as c:
        csrf = anon_csrf(c, path)
        good = c.post(path, data={"csrf_token": csrf, "new_password": PASSWORD,
                                  "confirm_password": PASSWORD})
        assert good.headers["location"] == "/account/two-step"


def test_invite_refused_for_disabled_user(app, admin):
    r = post(admin, "/users/local", email="ann@example.com", role="viewer", method="invite")
    path = secret_after(r.text, "Invite link").removeprefix("https://dash.example.com")
    ann = app.state.users.by_email("local", "ann@example.com")
    post(admin, f"/users/{ann.id}/disable")
    with anonymous(app) as c:
        assert c.get(path).status_code == 410
        csrf = anon_csrf(c, "/auth/login")
        assert c.post(path, data={"csrf_token": csrf, "new_password": PASSWORD,
                                  "confirm_password": PASSWORD}).status_code == 410
        assert c.get("/", headers=HTML).status_code == 302
    assert app.state.users.get(ann.id).password_hash is None


def _session_count(app, user_id):
    with app.state.store._lock:
        return app.state.store._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0]


def _accept(app, c, token):
    path = f"/auth/invite/{token}"
    csrf = anon_csrf(c, path)
    return c.post(path, data={"csrf_token": csrf, "new_password": PASSWORD,
                              "confirm_password": PASSWORD})


def test_reissued_invite_ends_the_users_other_sessions(app):
    ann = local(app)
    app.state.sessions.create({"user_id": ann.id, "csrf": "x"})  # an earlier device
    assert _session_count(app, ann.id) == 1
    token = app.state.users.create_invite(ann.id)
    with anonymous(app) as c:
        r = _accept(app, c, token)
        assert r.status_code == 303 and r.headers["location"] == "/"
    assert _session_count(app, ann.id) == 1  # only the new one


def test_reissued_invite_for_two_step_user_still_asks_for_the_code(app):
    ann = local(app)
    app.state.users.set_totp(ann.id, "JBSWY3DPEHPK3PXP", True)
    app.state.sessions.create({"user_id": ann.id, "csrf": "x"})
    token = app.state.users.create_invite(ann.id)
    with anonymous(app) as c:
        r = _accept(app, c, token)
        assert r.status_code == 303 and r.headers["location"] == "/auth/totp"
        assert c.get("/", headers=HTML).status_code == 302  # not signed in yet
        assert _session_count(app, ann.id) == 0  # the earlier device is out too
        csrf = anon_csrf(c, "/auth/totp")
        ok = c.post("/auth/totp", data={"csrf_token": csrf,
                                        "code": pyotp.TOTP("JBSWY3DPEHPK3PXP").now()})
        assert ok.status_code == 303 and ok.headers["location"] == "/"
        assert c.get("/", headers=HTML).status_code == 200


def test_invite_route_refuses_disabled_user(app, admin):
    ann = local(app)
    app.state.users.update(ann.id, disabled=True)
    r = post(admin, f"/users/{ann.id}/invite")
    assert r.status_code == 400 and "/auth/invite/" not in r.text


def test_invite_redirects_to_setup_while_setup_open(app):
    app.state.setup.is_open = lambda: True
    with anonymous(app) as c:
        r = c.get("/auth/invite/whatever")
        assert r.status_code == 303 and r.headers["location"] == "/setup"
        csrf = anon_csrf(c, "/auth/login")
        r = c.post("/auth/invite/whatever", data={"csrf_token": csrf, "new_password": PASSWORD,
                                                  "confirm_password": PASSWORD})
        assert r.status_code == 303 and r.headers["location"] == "/setup"


def test_invite_without_public_url_shows_path_and_warning(app, admin):
    app.state.settings = app.state.settings.model_copy(update={"public_url": None})
    r = post(admin, "/users/local", email="ann@example.com", role="viewer", method="invite")
    assert "/auth/invite/" in r.text and "dashboard address" in r.text
    assert "https://" not in secret_after(r.text, "Invite link")


def test_reset_password_gives_one_time_password_and_ends_sessions(app, admin):
    ann = local(app)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        set_session(c, app, {"user_id": ann.id, "csrf": CSRF})
        r = post(admin, f"/users/{ann.id}/reset-password")
        assert c.get("/", headers=HTML).status_code == 302
    pw = secret_after(r.text, "One-time password")
    rec = app.state.users.get(ann.id)
    assert rec.must_change_password and verify_password(rec.password_hash, pw)


def test_reset_two_step_forces_reenrolment_for_local_admin(app, admin):
    boss = other_admin(app)
    app.state.users.set_recovery_codes(boss.id, ["x"])
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        set_session(c, app, {"user_id": boss.id, "csrf": CSRF})
        assert c.get("/", headers=HTML).status_code == 200
        post(admin, f"/users/{boss.id}/reset-two-step")
        rec = app.state.users.get(boss.id)
        assert not rec.totp_enabled and rec.recovery_codes == [] and rec.totp_secret is None
        assert c.get("/", headers=HTML).status_code == 302  # sessions ended
        set_session(c, app, {"user_id": boss.id, "csrf": CSRF})
        assert c.get("/", headers=HTML).status_code == 302
        assert c.get("/", headers=HTML).headers["location"] == "/account/two-step"


def test_delete_requires_typed_email(app, admin):
    ann = local(app)
    r = post(admin, f"/users/{ann.id}/delete", confirm_email="wrong@example.com")
    assert r.status_code == 400 and "nothing was deleted" in r.text
    assert app.state.users.get(ann.id) is not None
    assert post(admin, f"/users/{ann.id}/delete",
                confirm_email="ANN@example.com").status_code == 303
    assert app.state.users.get(ann.id) is None


def test_add_entra_user_is_pinned(app, admin):
    assert post(admin, "/users/entra", upn="eve@corp.example", role="operator").status_code == 303
    rec = app.state.users.by_email("entra", "eve@corp.example")
    assert rec.role == "operator" and rec.role_pinned
    assert post(admin, "/users/entra", upn="eve@corp.example", role="viewer").status_code == 400


def test_operator_gets_403_on_every_users_route(app):
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        assert c.get("/users", headers=HTML).status_code == 403
        for path in ("/users/local", "/users/entra", "/users/1/role", "/users/1/unpin",
                     "/users/1/disable", "/users/1/enable", "/users/1/reset-password",
                     "/users/1/reset-two-step", "/users/1/sign-out", "/users/1/delete"):
            assert post(c, path).status_code == 403, path


def test_posts_need_csrf(admin):
    assert admin.post("/users/local", data={"email": "a@example.com"}).status_code == 403


def test_routes_still_guarded(app):
    assert_all_routes_guarded(app)


# --- account ---------------------------------------------------------------------

def test_account_page_shows_details_for_every_role_including_pending(app):
    for role in Role:
        with logged_in_client(app, role) as c:
            r = c.get("/account")
            assert r.status_code == 200 and "test@example.com" in r.text
            assert "/account/password" in r.text and "Two-step sign-in" in r.text


def test_account_for_entra_user_has_no_password_links(app):
    rec = app.state.users.create("entra", "e@example.com", "E", "viewer")
    with TestClient(app, base_url="https://testserver") as c:
        set_session(c, app, {"user_id": rec.id, "csrf": CSRF})
        r = c.get("/account")
    assert r.status_code == 200 and "/account/password" not in r.text
    assert "Sign out everywhere" in r.text


def test_recovery_codes_regenerate_and_old_ones_fail(app):
    with logged_in_client(app, Role.ADMIN, follow_redirects=False) as c:
        me = app.state.users.by_email("local", "test@example.com")
        app.state.users.set_password_hash(me.id, hash_password(PASSWORD), False)
        old = totp.new_recovery_codes()
        app.state.users.set_recovery_codes(me.id, [totp.hash_recovery_code(x) for x in old])
        assert post(c, "/account/recovery-codes", current_password="wrong").status_code == 400
        assert app.state.users.get(me.id).recovery_codes == [totp.hash_recovery_code(x) for x in old]
        r = post(c, "/account/recovery-codes", current_password=PASSWORD)
        new = re.findall(r"<code>([a-z0-9]{5}-[a-z0-9]{5})</code>", r.text)
        assert len(new) == 10 and not set(new) & set(old)
        assert "no-store" in r.headers["cache-control"]
        users = app.state.users
        assert not users.use_recovery_code(me.id, totp.hash_recovery_code(old[0]))
        assert users.use_recovery_code(me.id, totp.hash_recovery_code(new[0]))
        assert "-" not in "".join(re.findall(r"<code>[^<]*</code>", c.get("/account").text))


def test_turn_off_two_step_refused_for_required_admin_allowed_otherwise(app):
    with logged_in_client(app, Role.ADMIN, follow_redirects=False) as c:
        me = app.state.users.by_email("local", "test@example.com")
        app.state.users.set_password_hash(me.id, hash_password(PASSWORD), False)
        r = post(c, "/account/two-step/off", current_password=PASSWORD)
        assert r.status_code == 400 and app.state.users.get(me.id).totp_enabled
        app.state.settings = app.state.settings.model_copy(update={"require_totp_admins": False})
        assert post(c, "/account/two-step/off", current_password="nope").status_code == 400
        assert post(c, "/account/two-step/off", current_password=PASSWORD).status_code == 200
        rec = app.state.users.get(me.id)
        assert not rec.totp_enabled and rec.recovery_codes == []


def test_sign_out_everywhere_kills_other_sessions_and_this_one(app):
    ann = local(app)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as a, \
            TestClient(app, base_url="https://testserver", follow_redirects=False) as b:
        set_session(a, app, {"user_id": ann.id, "csrf": CSRF})
        set_session(b, app, {"user_id": ann.id, "csrf": CSRF})
        r = post(a, "/account/sign-out-everywhere")
        assert r.status_code == 303
        assert a.get("/", headers=HTML).status_code == 302
        assert b.get("/", headers=HTML).status_code == 302


def test_no_secrets_in_any_rendered_page(app, admin):
    boss = other_admin(app)
    ann = local(app)
    app.state.users.set_totp(ann.id, "JBSWY3DPEHPK3PXP", True)
    app.state.users.set_recovery_codes(ann.id, [totp.hash_recovery_code("abcde-fghij")])
    pages = [admin.get("/users").text, admin.get("/account").text]
    with TestClient(app, base_url="https://testserver") as c:
        set_session(c, app, {"user_id": ann.id, "csrf": CSRF})
        pages.append(c.get("/account").text)
    forbidden = [ann.password_hash, boss.password_hash or "\0", "JBSWY3DPEHPK3PXP",
                 totp.hash_recovery_code("abcde-fghij"), "$argon2"]
    for html in pages:
        for bad in forbidden:
            assert bad not in html


# --- review fixes -------------------------------------------------------------------

def test_invite_refused_while_local_sign_in_is_off(app, admin):
    r = post(admin, "/users/local", email="ann@example.com", role="viewer", method="invite")
    path = secret_after(r.text, "Invite link").removeprefix("https://dash.example.com")
    app.state.settings = app.state.settings.model_copy(update={"local_sign_in": False})
    with anonymous(app) as c:
        assert c.get(path).status_code == 403
        set_session(c, app, {"csrf": CSRF})  # the sign-in page has no form to read one from
        r = c.post(path, data={"csrf_token": CSRF, "new_password": PASSWORD,
                               "confirm_password": PASSWORD})
        assert r.status_code == 403 and "Local sign-in is turned off" in r.text
        assert c.get("/", headers=HTML).status_code == 302
    assert app.state.users.by_email("local", "ann@example.com").password_hash is None


def test_creating_local_users_refused_while_local_sign_in_is_off(app, admin):
    app.state.settings = app.state.settings.model_copy(update={"local_sign_in": False})
    for method in ("invite", "password"):
        r = post(admin, "/users/local", email="ann@example.com", role="viewer", method=method)
        assert r.status_code == 400 and "Local sign-in is turned off" in r.text
    assert app.state.users.by_email("local", "ann@example.com") is None
    ann = local(app, "bob@example.com")
    assert post(admin, f"/users/{ann.id}/invite").status_code == 400


def _invite_path(app, admin, email="ann@example.com"):
    r = post(admin, "/users/local", email=email, role="viewer", method="invite")
    return secret_after(r.text, "Invite link").removeprefix("https://dash.example.com")


def _dead(app, path):
    with anonymous(app) as c:
        return c.get(path).status_code == 410


def test_new_invite_replaces_the_old_one(app, admin):
    old = _invite_path(app, admin)
    ann = app.state.users.by_email("local", "ann@example.com")
    r = post(admin, f"/users/{ann.id}/invite")
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
    new = secret_after(r.text, "Invite link").removeprefix("https://dash.example.com")
    assert new != old and _dead(app, old)
    with anonymous(app) as c:
        assert c.get(new).status_code == 200


def test_invite_route_is_local_only(app, admin):
    rec = app.state.users.create("entra", "e@example.com", "E", "viewer")
    assert post(admin, f"/users/{rec.id}/invite").status_code == 400
    assert post(admin, "/users/999/invite").status_code == 404


def test_outstanding_invites_die_on_reset_password_disable_and_delete(app, admin):
    for action in ("reset-password", "disable", "delete"):
        email = f"{action}@example.com"
        path = _invite_path(app, admin, email)
        rec = app.state.users.by_email("local", email)
        data = {"confirm_email": email} if action == "delete" else {}
        assert post(admin, f"/users/{rec.id}/{action}", **data).status_code in (200, 303)
        assert _dead(app, path), action


def test_clear_two_step_clears_everything_in_one_go(app):
    ann = local(app)
    users = app.state.users
    users.set_totp(ann.id, "JBSWY3DPEHPK3PXP", True)
    users.set_totp_last_step(ann.id, 5)
    users.set_recovery_codes(ann.id, ["x", "y"])
    users.clear_two_step(ann.id)
    rec = users.get(ann.id)
    assert (rec.totp_secret, rec.totp_enabled, rec.totp_last_step, rec.recovery_codes) == (
        None, False, None, [])
    with pytest.raises(KeyError):
        users.clear_two_step(9999)
