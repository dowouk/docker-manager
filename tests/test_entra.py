"""Microsoft sign-in from the Settings page: token checks, user matching, role mapping."""
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from authlib.common.errors import AuthlibBaseError
from authlib.integrations.base_client import OAuthError
from fastapi.testclient import TestClient
from joserfc.errors import JoseError

from app.config import Settings
from app.entra import EntraIdentity, sign_in_entra, verified_identity
from app.main import create_app
from app.roles import Role
from app.store import Store
from auth_helpers import CSRF, assert_all_routes_guarded, sign_in

GROUP_A = "11111111-1111-1111-1111-111111111111"
GROUP_B = "22222222-2222-2222-2222-222222222222"
NEW_TENANT = "33333333-3333-3333-3333-333333333333"
HTML = {"Accept": "text/html"}
METADATA = {
    "issuer": "https://login.microsoftonline.com/tenant/v2.0",
    "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
    "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
    "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
}
WELL_KNOWN = "https://login.microsoftonline.com/{}/v2.0/.well-known/openid-configuration"


def claims_for(settings, **over):
    claims = {
        "tid": settings.azure_tenant_id,
        "iss": f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0",
        "aud": settings.azure_client_id,
        "oid": "oid-alice",
        "preferred_username": "alice@example.com",
        "name": "Alice",
    }
    claims.update(over)
    return claims


@pytest.fixture
def app(settings, tmp_path):
    app = create_app(
        settings, docker_client=object(), http=httpx.AsyncClient(),
        store=Store(str(tmp_path / "data" / "dashboard.db")), start_scheduler=False,
    )
    yield app
    app.state.store.close()


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


def set_default(app, value):
    app.state.settings = app.state.settings.model_copy(update={"default_role": value})


def map_group(app, group, role):
    svc = app.state.settings_service
    svc.save_group_mappings(svc.group_mappings() + [{"group_id": group, "role": role}],
                            "admin@x", "mapped")


def callback(client, app, settings, **over):
    app.state.oauth.entra.authorize_access_token = AsyncMock(
        return_value={"userinfo": claims_for(settings, **over)})
    return client.get("/auth/callback?code=1&state=2", headers=HTML)


def record(app, email="alice@example.com"):
    return app.state.users.by_email("entra", email)


# ---- verified_identity ----

def test_identity_accepts_good_claims(settings):
    ident = verified_identity(claims_for(settings, roles=["DockerManager.Admin"],
                                         groups=[GROUP_A]), settings)
    assert ident == EntraIdentity("tenant", "oid-alice", "alice@example.com", "Alice",
                                  ["DockerManager.Admin"], [GROUP_A])
    assert verified_identity(claims_for(settings), settings).groups is None


def test_identity_checks(settings):
    bad = [claims_for(settings, tid="other"), claims_for(settings, aud="other"),
           claims_for(settings, iss="https://evil.example/v2.0"),
           claims_for(settings, aud=["other"]),
           claims_for(settings, aud=["client", "other"]),
           claims_for(settings, aud=["client", "other"], azp="other"),
           claims_for(settings, oid=None), claims_for(settings, oid=""),
           claims_for(settings, preferred_username=None), "nonsense", None]
    for c in bad:
        assert verified_identity(c, settings) is None, c
    assert verified_identity(claims_for(settings, aud=["client", "x"], azp="client"), settings)
    # falls back to email; allow-lists from 0.2 are ignored
    settings.allowed_emails = ["someone@else.com"]
    settings.allowed_group_id = "g"
    only_email = claims_for(settings, preferred_username=None, email="a@b.com")
    got = verified_identity(only_email, settings)
    assert got.upn == "a@b.com" and got.matchable is False


# ---- sign_in_entra: new users, roles ----

def ident(settings, **over):
    return verified_identity(claims_for(settings, **over), settings)


def test_new_user_gets_default_role_unpinned(app, settings):
    rec = sign_in_entra(app.state.users, ident(settings), settings, [])
    assert (rec.role, rec.role_pinned, rec.kind) == ("viewer", False, "entra")
    assert (rec.entra_tid, rec.entra_oid) == ("tenant", "oid-alice")
    entries = [e.summary for e in app.state.store.list_audit()]
    assert "User created from Microsoft sign-in: alice@example.com (viewer)" in entries


def test_app_role_and_group_mapping(app, settings):
    users = app.state.users
    r = sign_in_entra(users, ident(settings, roles=["DockerManager.Admin"]), settings, [])
    assert r.role == "admin"
    maps = [{"group_id": GROUP_A, "role": "operator"}]
    r = sign_in_entra(users, ident(settings, oid="o2", preferred_username="b@example.com",
                                   roles=["DockerManager.Viewer"], groups=[GROUP_A]),
                      settings, maps)
    assert r.role == "operator"


def test_legacy_default_creates_pinned_admin(app, settings):
    s = settings.model_copy(update={"default_role": "admin_legacy"})
    rec = sign_in_entra(app.state.users, ident(s), s, [])
    assert (rec.role, rec.role_pinned) == ("admin", True)


def test_pinned_user_keeps_role(app, settings):
    users = app.state.users
    rec = users.create("entra", "alice@example.com", "Alice", "operator", pinned=True)
    users.bind_entra(rec.id, "tenant", "oid-alice")
    r = sign_in_entra(users, ident(settings, roles=["DockerManager.Admin"]), settings, [])
    assert r.role == "operator"


def test_unpinned_recomputed_and_audited(app, settings):
    users = app.state.users
    sign_in_entra(users, ident(settings, roles=["DockerManager.Operator"]), settings, [])
    r = sign_in_entra(users, ident(settings, roles=[]), settings, [])
    assert r.role == "viewer"
    entries = [e.summary for e in app.state.store.list_audit()]
    assert "Role changed by Entra mapping: alice@example.com operator → viewer" in entries


def test_last_admin_is_kept_with_warning(app, settings, caplog):
    users = app.state.users
    sign_in_entra(users, ident(settings, roles=["DockerManager.Admin"]), settings, [])
    with caplog.at_level(logging.WARNING, logger="app.entra"):
        r = sign_in_entra(users, ident(settings, roles=[]), settings, [])
    assert r.role == "admin"
    assert "last active Admin" in caplog.text
    # with another active admin the demotion goes through
    users.create("local", "boss@example.com", "Boss", "admin")
    assert sign_in_entra(users, ident(settings, roles=[]), settings, []).role == "viewer"


def test_precreated_user_binds_oid_and_matches_after_upn_change(app, settings):
    users = app.state.users
    pre = users.create("entra", "Alice@Example.com", "Alice", "operator", pinned=True)
    r = sign_in_entra(users, ident(settings), settings, [])
    assert r.id == pre.id and r.entra_oid == "oid-alice"
    again = sign_in_entra(users, ident(settings, preferred_username="alice.new@example.com"),
                          settings, [])
    assert again.id == pre.id and again.email == "alice.new@example.com"
    assert users.count() == 1


def test_same_upn_other_person_is_refused(app, settings):
    users = app.state.users
    sign_in_entra(users, ident(settings), settings, [])
    assert sign_in_entra(users, ident(settings, oid="someone-else"), settings, []) is None
    assert users.count() == 1


def test_disabled_user_refused(app, settings):
    users = app.state.users
    rec = sign_in_entra(users, ident(settings), settings, [])
    users.update(rec.id, disabled=True)
    assert sign_in_entra(users, ident(settings), settings, []) is None


def test_duplicate_on_concurrent_first_sign_in_continues(app, settings, monkeypatch):
    users = app.state.users
    winner = sign_in_entra(users, ident(settings), settings, [])
    real = users.by_entra
    calls = []

    def stale(tid, oid):  # the first lookup runs before the other request's insert
        calls.append(1)
        return None if len(calls) == 1 else real(tid, oid)

    def stale_email(kind, email):
        return None if len(calls) == 1 else users.__class__.by_email(users, kind, email)

    monkeypatch.setattr(users, "by_entra", stale)
    monkeypatch.setattr(users, "by_email", stale_email)
    r = sign_in_entra(users, ident(settings), settings, [])
    assert r.id == winner.id and users.count() == 1


# ---- login and callback routes ----

@respx.mock
def test_login_redirects_with_pkce_and_configured_redirect(client):
    respx.get(WELL_KNOWN.format("tenant")).mock(return_value=httpx.Response(200, json=METADATA))
    r = client.get("/auth/entra", headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(METADATA["authorization_endpoint"])
    assert "code_challenge_method=S256" in loc and "state=" in loc and "nonce=" in loc
    assert "redirect_uri=https%3A%2F%2Fdash.example.com%2Fauth%2Fcallback" in loc
    assert "evil.example" not in loc


def test_not_set_up_is_404_page(tmp_path, monkeypatch, settings):
    for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "PUBLIC_URL"):
        monkeypatch.delenv(k)
    app = create_app(Settings(_env_file=None), docker_client=object(), http=httpx.AsyncClient(),
                     store=Store(str(tmp_path / "x" / "dashboard.db")), start_scheduler=False)
    try:
        assert app.state.oauth is None
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
            for path in ("/auth/entra", "/auth/callback"):
                r = c.get(path)
                assert r.status_code == 404 and "Microsoft sign-in isn't set up" in r.text
    finally:
        app.state.store.close()


def test_no_public_url_means_not_set_up(tmp_path, monkeypatch, settings):
    monkeypatch.delenv("PUBLIC_URL")
    app = create_app(Settings(_env_file=None), docker_client=object(), http=httpx.AsyncClient(),
                     store=Store(str(tmp_path / "x" / "dashboard.db")), start_scheduler=False)
    try:
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
            r = c.get("/auth/entra", headers={"X-Forwarded-Host": "evil.example"})
            assert r.status_code == 404 and "Set the dashboard address" in r.text
            assert "location" not in r.headers and "evil.example" not in r.text
    finally:
        app.state.store.close()


def test_callback_new_user_signs_in_with_minimal_session(app, client, settings):
    r = callback(client, app, settings, roles=["DockerManager.Admin"], extra="secret")
    assert r.status_code == 303 and r.headers["location"] == "/"
    flags = r.headers["set-cookie"].lower()
    assert "httponly" in flags and "secure" in flags and "samesite=lax" in flags
    data = app.state.sessions.load(client.cookies["session"])
    assert set(data) == {"user_id", "csrf"}
    assert app.state.users.get(data["user_id"]).role == "admin"
    assert client.get("/", headers=HTML).status_code == 200
    entries = [e.summary for e in app.state.store.list_audit()]
    assert "Signed in (Microsoft)" in entries


def test_callback_replaces_planted_session(app, client, settings):
    from auth_helpers import set_session
    set_session(client, app, {"planted": "x"})
    callback(client, app, settings)
    data = app.state.sessions.load(client.cookies["session"])
    assert "planted" not in data


def test_callback_pending_lands_on_pending(app, client, settings):
    set_default(app, "pending")
    r = callback(client, app, settings)
    assert r.status_code == 303
    r = client.get("/", headers=HTML)
    assert r.status_code == 302 and r.headers["location"] == "/pending"
    assert "waiting for an Admin" in client.get("/pending", headers=HTML).text


def test_callback_other_tenant_refused(app, client, settings):
    r = callback(client, app, settings, tid="other")
    assert r.status_code == 403 and "You don't have access to this dashboard" in r.text
    assert client.get("/", headers={"Accept": "application/json"}).status_code == 401
    assert app.state.users.count() == 0


def test_callback_disabled_user_refused(app, client, settings):
    rec = sign_in_entra(app.state.users, ident(settings), settings, [])
    app.state.users.update(rec.id, disabled=True)
    assert callback(client, app, settings).status_code == 403


@pytest.mark.parametrize("exc", [
    JoseError("bad"), AuthlibBaseError("x"), OAuthError("bad"), httpx.ConnectError("down")])
def test_callback_errors_are_400(app, client, settings, exc):
    app.state.oauth.entra.authorize_access_token = AsyncMock(side_effect=exc)
    r = client.get("/auth/callback?code=1&state=2")
    assert r.status_code == 400 and "Sign-in failed" in r.text
    assert client.get("/", headers={"Accept": "application/json"}).status_code == 401


def test_callback_missing_userinfo_refused(app, client):
    app.state.oauth.entra.authorize_access_token = AsyncMock(return_value={})
    assert client.get("/auth/callback?code=1&state=2").status_code == 403


def test_callback_uses_saved_group_mappings(app, client, settings):
    map_group(app, GROUP_A, "operator")
    callback(client, app, settings, groups=[GROUP_A])
    assert record(app).role == "operator"


def test_setup_flow_uses_hook(app, client, settings):
    from auth_helpers import set_session
    seen = []

    def hook(identity):
        seen.append(identity.upn)
        return app.state.users.create_first_admin("entra", identity.upn, identity.name)

    app.state.entra_setup_hook = hook
    set_session(client, app, {"setup_entra": True})
    r = callback(client, app, settings)
    assert r.status_code == 303 and seen == ["alice@example.com"]
    assert record(app).role == "admin"


def test_setup_flow_without_hook_refused(app, client, settings):
    from auth_helpers import set_session
    app.state.entra_setup_hook = None
    set_session(client, app, {"setup_entra": True})
    # setup is closed in this app, so the clear message, not "no access"
    r = callback(client, app, settings)
    assert r.status_code == 409 and "Setup has already been completed" in r.text
    assert app.state.users.count() == 0


def test_routes_stay_guarded(app):
    assert_all_routes_guarded(app)


# ---- settings take effect without a restart ----

@respx.mock
def test_settings_change_applies_to_next_login(tmp_path, monkeypatch, settings):
    for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "PUBLIC_URL"):
        monkeypatch.delenv(k)
    app = create_app(Settings(_env_file=None), docker_client=object(), http=httpx.AsyncClient(),
                     store=Store(str(tmp_path / "x" / "dashboard.db")), start_scheduler=False)
    try:
        route = respx.get(WELL_KNOWN.format(NEW_TENANT)).mock(
            return_value=httpx.Response(200, json={
                **METADATA, "authorization_endpoint": "https://login.microsoftonline.com/" + NEW_TENANT + "/authorize"}))
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
            assert c.get("/auth/entra").status_code == 404
            sign_in(c, app)
            for section, form in (
                ("general", {"server_name": "x", "tz": "Europe/London",
                             "public_url": "https://new.example.com"}),
                ("signin", {"local_sign_in": "on", "default_role": "viewer",
                            "azure_tenant_id": NEW_TENANT, "azure_client_id": "newclient",
                            "azure_client_secret": "newsecret"}),
            ):
                r = c.post(f"/settings/{section}", data={**form, "csrf_token": CSRF})
                assert r.status_code == 303, r.text
            r = c.get("/auth/entra")
            assert r.status_code == 302 and route.called
            assert "client_id=newclient" in r.headers["location"]
            assert "new.example.com%2Fauth%2Fcallback" in r.headers["location"]
    finally:
        app.state.store.close()


# ---- Settings page: test button, redirect URI, group mappings ----

@respx.mock
def test_test_button_success(app, settings):
    respx.get(WELL_KNOWN.format("tenant")).mock(return_value=httpx.Response(200, json=METADATA))
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        sign_in(c, app)
        r = c.post("/settings/signin/test", data={"csrf_token": CSRF})
        assert r.status_code == 303 and r.headers["location"] == "/settings?msg=entra_ok#signin"
        assert "Microsoft sign-in settings look right" in c.get(r.headers["location"]).text


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(200, json={**METADATA, "issuer": "https://other/v2.0"}),
    httpx.Response(400, json={"error": "x"}),
    httpx.Response(200, text="not json"),
    httpx.ConnectError("down"),
])
def test_test_button_failure(app, response, caplog):
    respx.get(WELL_KNOWN.format("tenant")).mock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=None if isinstance(response, Exception) else response)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        sign_in(c, app)
        r = c.post("/settings/signin/test", data={"csrf_token": CSRF})
        assert r.headers["location"] == "/settings?msg=entra_failed#signin"
        page = c.get(r.headers["location"]).text
        assert "Couldn&#39;t reach Microsoft with these settings – check the tenant ID" in page
    assert "secret" not in caplog.text


def test_test_button_needs_admin_and_csrf(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        sign_in(c, app, Role.OPERATOR)
        assert c.post("/settings/signin/test", data={"csrf_token": CSRF}).status_code == 403
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        sign_in(c, app)
        assert c.post("/settings/signin/test").status_code == 403


def test_settings_page_redirect_uri_and_no_secret(app, settings):
    with TestClient(app, base_url="https://testserver") as c:
        sign_in(c, app)
        page = c.get("/settings").text
        assert 'value="https://dash.example.com/auth/callback"' in page and "data-copy" in page
        assert "Test Microsoft sign-in settings" in page
        assert 'value="secret"' not in page and ">secret<" not in page


def test_settings_page_without_public_url(app):
    with TestClient(app, base_url="https://testserver") as c:
        sign_in(c, app)
        app.state.settings = app.state.settings.model_copy(update={"public_url": None})
        assert "Set the dashboard address first" in c.get("/settings").text


def test_group_mapping_add_remove(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        sign_in(c, app)
        r = c.post("/settings/group-mappings",
                   data={"group_id": GROUP_A.upper(), "role": "operator", "csrf_token": CSRF})
        assert r.status_code == 303
        assert app.state.settings_service.group_mappings() == [
            {"group_id": GROUP_A, "role": "operator"}]
        assert GROUP_A in c.get("/settings").text
        # changing the role replaces rather than duplicates
        c.post("/settings/group-mappings",
               data={"group_id": GROUP_A, "role": "admin", "csrf_token": CSRF})
        assert app.state.settings_service.group_mappings() == [
            {"group_id": GROUP_A, "role": "admin"}]
        r = c.post(f"/settings/group-mappings/{GROUP_A}/delete", data={"csrf_token": CSRF})
        assert r.status_code == 303 and app.state.settings_service.group_mappings() == []
        summaries = [e.summary for e in app.state.store.list_audit()]
        assert any("role removed" in s for s in summaries)
        assert any("mapped to" in s for s in summaries)


def test_group_mapping_rejects_bad_input(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        sign_in(c, app)
        for data in ({"group_id": "nope", "role": "viewer"},
                     {"group_id": GROUP_B, "role": "pending"}):
            r = c.post("/settings/group-mappings", data={**data, "csrf_token": CSRF})
            assert r.status_code == 400 and 'role="alert"' in r.text
        assert app.state.settings_service.group_mappings() == []


def test_group_mapping_needs_admin_and_csrf(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        sign_in(c, app, Role.OPERATOR)
        data = {"group_id": GROUP_A, "role": "viewer", "csrf_token": CSRF}
        assert c.post("/settings/group-mappings", data=data).status_code == 403
        assert c.post(f"/settings/group-mappings/{GROUP_A}/delete",
                      data={"csrf_token": CSRF}).status_code == 403
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        sign_in(c, app)
        assert c.post("/settings/group-mappings", data={"group_id": GROUP_A,
                                                       "role": "viewer"}).status_code == 403


# ---- review fixes ----

def test_email_only_token_does_not_bind_precreated_account(app, settings, caplog):
    users = app.state.users
    pre = users.create("entra", "victim@example.com", "Victim", "admin", pinned=True)
    only_email = ident(settings, oid="attacker", preferred_username=None,
                       email="victim@example.com")
    with caplog.at_level(logging.WARNING, logger="app.entra"):
        assert sign_in_entra(users, only_email, settings, []) is None
    assert users.get(pre.id).entra_oid is None and users.count() == 1
    assert "preferred_username" in caplog.text


def test_email_only_token_new_user_is_created(app, settings):
    rec = sign_in_entra(app.state.users, ident(settings, preferred_username=None,
                                               email="fresh@example.com"), settings, [])
    assert rec.email == "fresh@example.com" and rec.entra_oid == "oid-alice"


def test_same_upn_different_oid_logs(app, settings, caplog):
    users = app.state.users
    sign_in_entra(users, ident(settings), settings, [])
    with caplog.at_level(logging.WARNING, logger="app.entra"):
        assert sign_in_entra(users, ident(settings, oid="other"), settings, []) is None
    assert "different Microsoft account" in caplog.text


def test_upn_refresh_clash_is_logged_and_sign_in_continues(app, settings, caplog):
    users = app.state.users
    sign_in_entra(users, ident(settings, oid="o1", preferred_username="a@example.com"),
                  settings, [])
    sign_in_entra(users, ident(settings, oid="o2", preferred_username="b@example.com"),
                  settings, [])
    with caplog.at_level(logging.WARNING, logger="app.entra"):
        r = sign_in_entra(users, ident(settings, oid="o2", preferred_username="a@example.com"),
                          settings, [])
    assert r.email == "b@example.com" and "another account uses it" in caplog.text


MAPS = [{"group_id": GROUP_A, "role": "operator"}]


def test_overage_keeps_unpinned_users_role(app, settings, caplog):
    users = app.state.users
    r = sign_in_entra(users, ident(settings, groups=[GROUP_A]), settings, MAPS)
    assert r.role == "operator"
    with caplog.at_level(logging.WARNING, logger="app.entra"):
        r = sign_in_entra(users, ident(settings), settings, MAPS)  # no groups claim
    assert r.role == "operator" and "No groups in the token" in caplog.text
    # a default above the group's role doesn't promote either
    s = settings.model_copy(update={"default_role": "operator"})
    v = sign_in_entra(users, ident(s, oid="o2", preferred_username="v@example.com",
                                   groups=[GROUP_B]), s, [{"group_id": GROUP_B, "role": "viewer"}])
    assert v.role == "viewer"
    v = sign_in_entra(users, ident(s, oid="o2", preferred_username="v@example.com"),
                      s, [{"group_id": GROUP_B, "role": "viewer"}])
    assert v.role == "viewer"


def test_overage_new_user_gets_app_role_or_default(app, settings):
    users = app.state.users
    r = sign_in_entra(users, ident(settings, roles=["DockerManager.Operator"]), settings, MAPS)
    assert r.role == "operator"
    r = sign_in_entra(users, ident(settings, oid="o2", preferred_username="n@example.com"),
                      settings, MAPS)
    assert r.role == "viewer"


def test_no_mappings_still_recomputes_without_groups(app, settings):
    users = app.state.users
    sign_in_entra(users, ident(settings, roles=["DockerManager.Operator"]), settings, [])
    assert sign_in_entra(users, ident(settings), settings, []).role == "viewer"


def test_recompute_pins_legacy_default(app, settings):
    users = app.state.users
    rec = users.create("entra", "alice@example.com", "Alice", "viewer")
    s = settings.model_copy(update={"default_role": "admin_legacy"})
    r = sign_in_entra(users, ident(s), s, [])
    assert r.id == rec.id and (r.role, r.role_pinned) == ("admin", True)


def test_disabled_second_admin_does_not_count(app, settings):
    users = app.state.users
    sign_in_entra(users, ident(settings, roles=["DockerManager.Admin"]), settings, [])
    other = users.create("local", "boss@example.com", "Boss", "admin")
    users.update(other.id, disabled=True)
    assert sign_in_entra(users, ident(settings), settings, []).role == "admin"


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(400, json={}), httpx.ConnectError("down")])
def test_login_metadata_failure_is_502_page(client, response, caplog):
    respx.get(WELL_KNOWN.format("tenant")).mock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=None if isinstance(response, Exception) else response)
    r = client.get("/auth/entra")
    assert r.status_code == 502 and "Sign-in failed" in r.text
    assert "secret" not in caplog.text


# ---- user_pending notification ----

def test_on_pending_called_only_for_a_new_pending_user(app, settings):
    seen = []
    s = settings.model_copy(update={"default_role": "pending"})
    sign_in_entra(app.state.users, ident(s), s, [], on_pending=lambda n, u: seen.append((n, u)))
    assert len(seen) == 1 and seen[0][1] == "alice@example.com"
    sign_in_entra(app.state.users, ident(s), s, [], on_pending=lambda n, u: seen.append((n, u)))
    assert len(seen) == 1  # signing in again isn't a new user
    other = ident(s, oid="o2", preferred_username="v@example.com", name="V")
    viewer = settings.model_copy(update={"default_role": "viewer"})
    sign_in_entra(app.state.users, other, viewer, [], on_pending=lambda n, u: seen.append(1))
    assert len(seen) == 1  # a viewer isn't waiting for approval


def test_on_pending_failure_does_not_break_sign_in(app, settings):
    s = settings.model_copy(update={"default_role": "pending"})

    def boom(name, upn):
        raise RuntimeError("nope")

    assert sign_in_entra(app.state.users, ident(s), s, [], on_pending=boom).role == "pending"


def test_callback_notifies_a_new_pending_user(app, client, settings):
    calls = []
    app.state.notifier.user_pending = lambda name, upn: calls.append((name, upn))
    set_default(app, "pending")
    callback(client, app, settings)
    assert len(calls) == 1 and calls[0][1] == "alice@example.com"
