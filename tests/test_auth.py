import os
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from authlib.common.errors import AuthlibBaseError
from joserfc.errors import InvalidClaimError, JoseError
from starlette.staticfiles import StaticFiles
from authlib.integrations.base_client import OAuthError
from fastapi import Depends, FastAPI, Form
from fastapi.testclient import TestClient
from httpx import Response

from app.auth import (
    User,
    csrf_token,
    current_user,
    setup_auth,
    user_from_claims,
    verify_csrf,
)
from app.sessions import SessionStore
from app.store import Store
from auth_helpers import CSRF, assert_all_routes_guarded, logged_in_client, set_session

CALLS: list[str] = []


def make_app(settings) -> FastAPI:
    app = FastAPI()
    app.state.sessions = SessionStore(Store(os.path.join(settings.data_dir, "dashboard.db")))
    setup_auth(app, settings, app.state.sessions)

    @app.get("/")
    def index(user: User = Depends(current_user)):
        CALLS.append("index")
        return {"user": user.email, "csrf": "x"}

    @app.post("/check")
    def check():
        CALLS.append("check")
        return {"ok": True}

    @app.get("/jobs/{job_id}/stream")
    def stream(job_id: str):
        CALLS.append("stream")
        return {"ok": True}

    @app.post("/act", dependencies=[Depends(verify_csrf)])
    def act(thing: str = Form("")):
        CALLS.append("act")
        return {"ok": True, "thing": thing}

    return app


@pytest.fixture(autouse=True)
def _clear_calls():
    CALLS.clear()


@pytest.fixture
def client_app(settings):
    app = make_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


@pytest.fixture
def logged_in_client_(settings):
    app = make_app(settings)
    with logged_in_client(app, settings, follow_redirects=False) as c:
        yield c


def good_claims(settings, **over):
    claims = {
        "tid": settings.azure_tenant_id,
        "iss": f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0",
        "aud": settings.azure_client_id,
        "preferred_username": "Alice@Example.com",
        "name": "Alice",
    }
    claims.update(over)
    return claims


# ---- guard ----

def test_every_route_requires_auth(settings):
    assert_all_routes_guarded(make_app(settings))
    assert CALLS == []


def test_unauthenticated_request_never_reaches_handler(client_app):
    for method, path in [("GET", "/"), ("POST", "/check"), ("POST", "/act")]:
        client_app.request(method, path, headers={"Accept": "text/html"})
    assert CALLS == []


def test_html_get_redirects_to_login(client_app):
    r = client_app.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 302 and r.headers["location"] == "/auth/login"


def test_non_html_or_post_gets_401_json(client_app):
    r = client_app.get("/", headers={"Accept": "application/json"})
    assert r.status_code == 401 and r.json() == {"detail": "Not signed in"}
    r = client_app.post("/check", headers={"Accept": "text/html"})
    assert r.status_code == 401 and r.json() == {"detail": "Not signed in"}


def test_sse_without_session_is_401(client_app):
    r = client_app.get("/jobs/1/stream", headers={"Accept": "text/event-stream"})
    assert r.status_code == 401
    assert CALLS == []


@pytest.mark.parametrize(
    "path",
    ["/authx", "/auth", "//auth/login", "/auth/../", "/auth/../check", "/static/../check",
     "/%2e%2e/check", "/auth/%2e%2e/check", "/auth/..%2fcheck", "/auth\\..\\check",
     "/Auth/login", "/staticx/a"],
)
def test_auth_prefix_tricks_do_not_bypass(client_app, path):
    r = client_app.post(path, headers={"Accept": "application/json"})
    assert r.status_code in (401, 404, 405)
    assert CALLS == []
    assert r.status_code != 200


@pytest.mark.parametrize("path", ["/authx", "//auth/login", "/auth/../check", "/static/../check",
                                  "/auth/%2e%2e/check"])
def test_tricks_are_guarded_with_401(client_app, path):
    r = client_app.get(path, headers={"Accept": "application/json"})
    assert r.status_code == 401


def test_logged_in_reaches_handler(logged_in_client_):
    r = logged_in_client_.get("/")
    assert r.status_code == 200 and r.json()["user"] == "test@example.com"


def test_unknown_session_token_rejected(settings):
    app = make_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        c.cookies.set("session", "not-a-real-token", domain="testserver.local", path="/")
        assert c.get("/", headers={"Accept": "application/json"}).status_code == 401
    assert CALLS == []


def test_old_signed_cookie_redirects_to_login(settings):
    app = make_app(settings)
    old = "eyJ1c2VyIjogeyJlbWFpbCI6ICJhQGV4YW1wbGUuY29tIn19.aBcDeF.xYz123-signature"
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        c.cookies.set("session", old, domain="testserver.local", path="/")
        r = c.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 302 and r.headers["location"] == "/auth/login"
    assert CALLS == []


def test_session_without_user_rejected(settings):
    app = make_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        set_session(c, app, {"csrf": CSRF})
        assert c.get("/", headers={"Accept": "application/json"}).status_code == 401


# ---- CSRF ----

def test_post_without_csrf_is_403(logged_in_client_):
    r = logged_in_client_.post("/act", data={"thing": "a"})
    assert r.status_code == 403 and CALLS == []


def test_post_with_wrong_csrf_is_403(logged_in_client_):
    assert logged_in_client_.post("/act", data={"csrf_token": "nope"}).status_code == 403
    assert logged_in_client_.post("/act", headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert CALLS == []


def test_post_with_csrf_form_field_ok(logged_in_client_):
    r = logged_in_client_.post("/act", data={"csrf_token": CSRF, "thing": "a"})
    assert r.status_code == 200 and r.json()["thing"] == "a"


def test_post_with_csrf_header_ok(logged_in_client_):
    r = logged_in_client_.post("/act", headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 200


def test_csrf_token_stored_in_session(settings):
    app = FastAPI()
    app.state.sessions = SessionStore(Store(os.path.join(settings.data_dir, "dashboard.db")))
    setup_auth(app, settings, app.state.sessions)

    from starlette.requests import Request

    @app.get("/t")
    def t(request: Request):
        return {"a": csrf_token(request), "b": csrf_token(request)}

    with logged_in_client(app, settings) as c:
        body = c.get("/t").json()
        assert body["a"] == body["b"] == CSRF


# ---- user_from_claims ----

def test_user_from_claims_accepts_good_claims(settings):
    assert user_from_claims(good_claims(settings), settings) == User("Alice", "Alice@Example.com")


def test_user_from_claims_falls_back_to_email_and_name(settings):
    claims = good_claims(settings, preferred_username=None, email="a@b.com", name=None)
    assert user_from_claims(claims, settings) == User("a@b.com", "a@b.com")


def test_user_from_claims_wrong_tenant_rejected(settings):
    assert user_from_claims(good_claims(settings, tid="other"), settings) is None
    claims = good_claims(settings)
    del claims["tid"]
    assert user_from_claims(claims, settings) is None


def test_user_from_claims_wrong_audience_or_issuer_rejected(settings):
    assert user_from_claims(good_claims(settings, aud="other"), settings) is None
    assert user_from_claims(good_claims(settings, aud=["other"]), settings) is None
    assert user_from_claims(good_claims(settings, aud=["x", "client"], azp="client"), settings)
    assert user_from_claims(good_claims(settings, iss="https://evil.example/v2.0"), settings) is None
    assert user_from_claims(
        good_claims(settings, iss="https://login.microsoftonline.com/other/v2.0"), settings
    ) is None
    claims = good_claims(settings)
    del claims["aud"]
    assert user_from_claims(claims, settings) is None


def test_user_from_claims_requires_identity(settings):
    claims = good_claims(settings, preferred_username=None)
    assert user_from_claims(claims, settings) is None
    assert user_from_claims("nonsense", settings) is None


def test_allowed_emails_enforced(settings):
    settings.allowed_emails = ["alice@example.com"]
    assert user_from_claims(good_claims(settings), settings) is not None  # case-insensitive
    bad = good_claims(settings, preferred_username="mallory@example.com")
    assert user_from_claims(bad, settings) is None


def test_allowed_emails_ignores_email_claim(settings):
    settings.allowed_emails = ["alice@example.com"]
    via_email = good_claims(settings, preferred_username="x@y.com", email="alice@example.com")
    assert user_from_claims(via_email, settings) is None
    no_pu = good_claims(settings, preferred_username=None, email="alice@example.com")
    assert user_from_claims(no_pu, settings) is None


def test_allowed_emails_empty_strings_never_match(settings):
    settings.allowed_emails = [""]
    assert user_from_claims(good_claims(settings), settings) is None
    settings.allowed_emails = ["", "  ", "alice@example.com"]
    assert user_from_claims(good_claims(settings), settings) is not None
    only_email = good_claims(settings, preferred_username="", email="a@b.com")
    assert user_from_claims(only_email, settings) is None


def test_multi_audience_requires_matching_azp(settings):
    multi = good_claims(settings, aud=["client", "other"])
    assert user_from_claims(multi, settings) is None
    assert user_from_claims({**multi, "azp": "other"}, settings) is None
    assert user_from_claims({**multi, "azp": "client"}, settings) is not None
    single = good_claims(settings, azp="other")
    assert user_from_claims(single, settings) is not None


def test_allowed_group_enforced(settings):
    settings.allowed_group_id = "group-1"
    assert user_from_claims(good_claims(settings, groups=["group-1"]), settings) is not None
    assert user_from_claims(good_claims(settings, groups=["group-2"]), settings) is None
    assert user_from_claims(good_claims(settings), settings) is None  # no groups: fail closed
    assert user_from_claims(good_claims(settings, groups="group-1"), settings) is None


def test_email_and_group_both_required_when_both_set(settings):
    settings.allowed_emails = ["alice@example.com"]
    settings.allowed_group_id = "group-1"
    assert user_from_claims(good_claims(settings, groups=["group-1"]), settings) is not None
    assert user_from_claims(good_claims(settings), settings) is None
    other = good_claims(settings, preferred_username="m@example.com", groups=["group-1"])
    assert user_from_claims(other, settings) is None


# ---- login / callback / logout ----

METADATA = {
    "issuer": "https://login.microsoftonline.com/tenant/v2.0",
    "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
    "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
    "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
}


@respx.mock
def test_login_redirects_to_entra_with_pkce(client_app):
    respx.get(
        "https://login.microsoftonline.com/tenant/v2.0/.well-known/openid-configuration"
    ).mock(return_value=Response(200, json=METADATA))
    r = client_app.get("/auth/login")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(METADATA["authorization_endpoint"])
    assert "code_challenge_method=S256" in loc and "code_challenge=" in loc
    assert "state=" in loc and "nonce=" in loc
    assert "redirect_uri=https%3A%2F%2Fdash.example.com%2Fauth%2Fcallback" in loc
    assert "scope=openid+profile+email" in loc or "scope=openid%20profile%20email" in loc


def _callback(settings, monkeypatch, userinfo=None, error=False):
    app = make_app(settings)
    mock = AsyncMock(side_effect=OAuthError("bad") if error else None,
                     return_value={"userinfo": userinfo})
    monkeypatch.setattr(app.state.oauth.entra, "authorize_access_token", mock)
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    # pre-existing (attacker-planted) session data must be discarded
    set_session(c, app, {"planted": "x"})
    c.app_ = app
    return c


def test_callback_success_sets_minimal_session(settings, monkeypatch):
    c = _callback(settings, monkeypatch, good_claims(settings, groups=["g"], extra="secret"))
    r = c.get("/auth/callback?code=1&state=2")
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "secure" in r.headers["set-cookie"].lower()
    assert "samesite=lax" in r.headers["set-cookie"].lower()
    r = c.get("/")
    assert r.status_code == 200 and r.json()["user"] == "Alice@Example.com"
    data = c.app_.state.sessions.load(c.cookies["session"])
    assert set(data) == {"user", "csrf"}
    assert data["user"] == {"name": "Alice", "email": "Alice@Example.com"}
    assert data["csrf"]


def test_callback_denied_gives_403_and_no_session(settings, monkeypatch):
    c = _callback(settings, monkeypatch, good_claims(settings, tid="other"))
    r = c.get("/auth/callback?code=1&state=2")
    assert r.status_code == 403
    assert "You don't have access to this dashboard" in r.text
    assert c.get("/", headers={"Accept": "application/json"}).status_code == 401


def test_callback_oauth_error_is_400_no_session(settings, monkeypatch):
    c = _callback(settings, monkeypatch, error=True)
    assert c.get("/auth/callback?error=x").status_code == 400
    assert c.get("/", headers={"Accept": "application/json"}).status_code == 401


def test_callback_missing_userinfo_denied(settings, monkeypatch):
    c = _callback(settings, monkeypatch, userinfo=None)
    assert c.get("/auth/callback?code=1&state=2").status_code == 403


def test_logout_is_post_with_csrf(logged_in_client_):
    c = logged_in_client_
    # GET must not log out
    assert c.get("/auth/logout").status_code == 405
    assert c.get("/").status_code == 200
    # POST without CSRF must not log out
    assert c.post("/auth/logout").status_code == 403
    assert c.get("/").status_code == 200
    r = c.post("/auth/logout", data={"csrf_token": CSRF})
    assert r.status_code == 302 and r.headers["location"] == "/auth/signed-out"
    page = c.get("/auth/signed-out")
    assert page.status_code == 200 and "Signed out" in page.text
    assert c.get("/", headers={"Accept": "application/json"}).status_code == 401


@pytest.mark.parametrize(
    "exc",
    [JoseError("bad token"), InvalidClaimError("exp"), AuthlibBaseError("x"), httpx.ConnectError("down"), httpx.HTTPStatusError(
        "500", request=httpx.Request("POST", "https://x"), response=httpx.Response(500))],
)
def test_callback_validation_and_network_errors_are_400(settings, monkeypatch, exc):
    app = make_app(settings)
    monkeypatch.setattr(
        app.state.oauth.entra, "authorize_access_token", AsyncMock(side_effect=exc)
    )
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    r = c.get("/auth/callback?code=1&state=2")
    assert r.status_code == 400 and "Sign-in failed" in r.text
    assert c.get("/", headers={"Accept": "application/json"}).status_code == 401


def test_helper_handles_static_mount(settings, tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    app = make_app(settings)
    app.mount("/static", StaticFiles(directory=tmp_path), name="static")
    assert_all_routes_guarded(app)
    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/static/a.txt").status_code == 200


def test_helper_flags_unexpected_auth_route(settings):
    app = make_app(settings)

    @app.get("/auth/debug")
    def debug():
        return {}

    with pytest.raises(AssertionError, match="unexpected /auth routes"):
        assert_all_routes_guarded(app)


def test_helper_covers_guarded_websocket_route(settings):
    from fastapi import WebSocket
    app = make_app(settings)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()

    assert_all_routes_guarded(app)  # guarded: must pass


def test_helper_fails_on_unguarded_http_route():
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)  # no setup_auth

    @app.get("/open")
    def open_route():
        return {}

    with pytest.raises(AssertionError, match="returned 200"):
        assert_all_routes_guarded(app)


def test_helper_fails_on_unguarded_websocket():
    from fastapi import WebSocket
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    with pytest.raises(AssertionError, match="accepted an unauthenticated"):
        assert_all_routes_guarded(app)


def test_helper_rejects_plain_route_under_static(settings):
    app = make_app(settings)

    @app.get("/static/admin")
    def admin():
        return {}

    with pytest.raises(AssertionError, match="not a StaticFiles mount"):
        assert_all_routes_guarded(app)


def test_login_without_entra_is_503_page(settings, monkeypatch):
    for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "PUBLIC_URL"):
        monkeypatch.delenv(k)
    from app.config import Settings

    app = make_app(Settings(_env_file=None))
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        r = c.get("/auth/login")
        assert r.status_code == 503
        assert "Microsoft sign-in isn't set up" in r.text
        assert c.get("/auth/signed-out").status_code == 200
        assert c.get("/").status_code == 401
