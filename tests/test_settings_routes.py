"""Settings page, test buttons and the History "Changes" view."""
import httpx
import pytest

from app.main import create_app
from app.roles import Role
from app.store import Store
from notify_fakes import FakeProvider, providers_for
from auth_helpers import CSRF, assert_all_routes_guarded, logged_in_client

SECRET_STRING = "endpoint=https://x;accesskey=TOPSECRET"
GOOD_DIGEST = {"csrf_token": CSRF, "digest_schedule": "0 8 * * MON"}
ENV_EMAIL = {"ACS_CONNECTION_STRING": SECRET_STRING, "DIGEST_FROM": "a@x.com", "DIGEST_TO": "b@x.com"}


class FakeRunner:
    def submit(self, *args):
        raise AssertionError("not used")


@pytest.fixture
def app(settings, tmp_path):
    app = create_app(
        settings,
        docker_client=object(),
        http=httpx.AsyncClient(),
        store=Store(str(tmp_path / "data" / "dashboard.db")),
        runner=FakeRunner(),
        start_scheduler=False,
    )
    yield app
    app.state.store.close()


@pytest.fixture
def client(app):
    with logged_in_client(app, follow_redirects=False) as c:
        yield c


def test_settings_page_lists_sections_and_locks_env(client):  # SERVER_NAME set in fixture env
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Set by the server's environment (<code>SERVER_NAME</code>)" in r.text
    for heading in ("General", "Checking", "Weekly digest", "Registry logins"):
        assert heading in r.text


def test_save_requires_csrf(client):
    r = client.post("/settings/checking", data={"check_interval_hours": "3"},
                    headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403


def test_save_applies_and_redirects(client, app):
    r = client.post("/settings/checking", data={
        "csrf_token": CSRF, "check_interval_hours": "3", "health_timeout_seconds": "300",
        "mailcow_dir": "/opt/mailcow-dockerized"})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?msg=saved#checking"
    assert app.state.settings.check_interval_hours == 3
    assert "Settings saved" in client.get("/settings?msg=saved").text


def test_invalid_rerenders_400_with_inputs(client):
    r = client.post("/settings/digest", data={
        "csrf_token": CSRF, "digest_schedule": "every monday", "digest_when_none": "on"})
    assert r.status_code == 400 and "every monday" in r.text
    assert "five-part cron schedule" in r.text


def test_secret_never_rendered(settings, tmp_path, monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("AZURE_CLIENT_SECRET")
    app = create_app(Settings(_env_file=None), docker_client=object(), http=httpx.AsyncClient(),
                     store=Store(str(tmp_path / "d.db")), runner=FakeRunner(),
                     start_scheduler=False)
    with logged_in_client(app, follow_redirects=False) as c:
        c.post("/settings/signin", data={"csrf_token": CSRF, "local_sign_in": "on",
                                         "default_role": "pending",
                                         "azure_client_secret": "TOPSECRET"})
        for url in ("/settings", "/history?view=changes"):
            assert "TOPSECRET" not in c.get(url).text
        assert "Saved – enter a new value to replace it" in c.get("/settings").text


def test_email_connection_fields_are_off_the_page(client):
    r = client.get("/settings")
    assert 'name="acs_connection_string"' not in r.text
    assert 'name="digest_from"' not in r.text and 'name="digest_to"' not in r.text
    assert 'name="digest_schedule"' in r.text


def test_secret_not_echoed_on_error(client):
    r = client.post("/settings/signin", data={
        "csrf_token": CSRF, "local_sign_in": "on", "default_role": "nope",
        "azure_client_secret": "TOPSECRET"})
    assert r.status_code == 400 and "TOPSECRET" not in r.text


def test_unknown_section_404(client):
    assert client.post("/settings/nope", data={"csrf_token": CSRF}).status_code == 404


@pytest.fixture
def email_settings(settings, monkeypatch):
    from app.config import Settings

    for k, v in ENV_EMAIL.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def _email_app(email_settings, tmp_path, provider):
    return create_app(email_settings, docker_client=object(), http=httpx.AsyncClient(),
                      store=Store(str(tmp_path / "d.db")), runner=FakeRunner(),
                      start_scheduler=False, providers=providers_for(provider))


def test_digest_send_failure_does_not_log_secret(email_settings, tmp_path, caplog):
    app = _email_app(email_settings, tmp_path,
                     FakeProvider(fail=RuntimeError("accesskey=TOPSECRET")))
    from datetime import datetime, timezone
    from app.checker import CheckResult
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    app.state.store.save_check(CheckResult(checked_at=now, containers=[]).to_dict(), now)
    with logged_in_client(app, follow_redirects=False) as c:
        r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_failed"
    assert "TOPSECRET" not in caplog.text and "RuntimeError" in caplog.text


def _add(client, host="ghcr.io", password="hunter2hunter2"):
    return client.post("/settings/registries", data={
        "csrf_token": CSRF, "host": host, "username": "ste", "password": password})


def test_registry_add_tests_login_first(client, app, respx_mock):
    respx_mock.get("https://ghcr.io/v2/").mock(return_value=httpx.Response(
        401, headers={"WWW-Authenticate": 'Basic realm="x"'}))
    r = _add(client)
    assert r.status_code == 400
    assert "The registry refused this username or password" in r.text
    assert 'value="ghcr.io"' in r.text and 'value="ste"' in r.text
    assert "hunter2hunter2" not in r.text
    assert app.state.settings_service.registry_logins() == []


def test_registry_add_and_delete(client, app, respx_mock):
    respx_mock.get("https://localhost:5000/v2/").mock(return_value=httpx.Response(200))
    respx_mock.get("https://ghcr.io/v2/").mock(return_value=httpx.Response(200))
    r = _add(client, host="https://GHCR.io/")
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?msg=registry_saved#registries"
    _add(client, host="localhost:5000")
    page = client.get("/settings").text
    assert "ghcr.io" in page and "localhost:5000" in page and "ste" in page
    assert "hunter2hunter2" not in page
    assert [x["host"] for x in app.state.settings_service.registry_logins()] == [
        "ghcr.io", "localhost:5000"]

    r = client.post("/settings/registries/localhost:5000/delete", data={"csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?msg=registry_removed#registries"
    assert [x["host"] for x in app.state.settings_service.registry_logins()] == ["ghcr.io"]
    summaries = [e.summary for e in app.state.store.list_audit()]
    assert "Registry login localhost:5000 removed" in summaries
    assert "Registry login ghcr.io added" in summaries


def test_registry_add_replaces_same_host(client, app, respx_mock):
    respx_mock.get("https://ghcr.io/v2/").mock(return_value=httpx.Response(200))
    _add(client)
    _add(client, password="another-password")
    logins = app.state.settings_service.registry_logins()
    assert len(logins) == 1 and logins[0]["password"] == "another-password"


def test_registry_bad_host(client):
    r = _add(client, host="a/b")
    assert r.status_code == 400 and "registry host" in r.text


def test_history_changes_view(client):
    client.post("/settings/checking", data={
        "csrf_token": CSRF, "check_interval_hours": "3", "health_timeout_seconds": "300",
        "mailcow_dir": "/opt/mailcow-dockerized"})
    page = client.get("/history?view=changes").text
    assert "Checking: Check every (hours) 6 → 3" in page
    assert "test@example.com" in page
    default = client.get("/history").text
    assert "Check every (hours)" not in default
    assert "Updates" in default and "Changes" in default


def test_history_changes_lede_and_kind_filter(app, client):
    store = app.state.store
    store.add_audit("a@x.com", "settings", "Checking: changed A")
    store.add_audit("b@x.com", "signin", "Sign-in failed: SIGNINROW")
    store.add_audit("c@x.com", "account", "Password changed ACCOUNTROW")
    store.add_audit("d@x.com", "users", "Added USERSROW")
    page = client.get("/history?view=changes").text
    assert "Every settings, sign-in and account change, newest first." in page
    assert all(m in page for m in ("SIGNINROW", "ACCOUNTROW", "USERSROW", "changed A"))
    only = client.get("/history?view=changes&kind=signin").text
    assert "SIGNINROW" in only and "ACCOUNTROW" not in only and "USERSROW" not in only
    assert "changed A" not in only
    # an unknown kind is ignored, not an error (and not a SQL filter)
    unknown = client.get("/history?view=changes&kind=setup").text
    assert all(m in unknown for m in ("SIGNINROW", "ACCOUNTROW", "USERSROW"))


def test_history_kind_filter_is_admin_only(app):
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        r = c.get("/history?view=changes&kind=signin", headers={"accept": "text/html"})
        assert r.status_code in (302, 403)


def test_all_routes_still_guarded(app):
    assert_all_routes_guarded(app)


def test_app_starts_with_a_bad_key_file_and_leaves_it_alone(settings, tmp_path, caplog, monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("AZURE_CLIENT_SECRET")
    settings = Settings(_env_file=None)
    key = tmp_path / "secret.key"  # DATA_DIR is tmp_path
    key.write_bytes(b"too short")
    app = create_app(
        settings, docker_client=object(), http=httpx.AsyncClient(),
        store=Store(str(tmp_path / "data" / "dashboard.db")),
        runner=FakeRunner(), start_scheduler=False,
    )
    assert key.read_bytes() == b"too short"
    assert "secret key" in caplog.text
    with logged_in_client(app, follow_redirects=False) as c:
        assert c.get("/settings").status_code == 200
        r = c.post("/settings/signin", data={
            "csrf_token": CSRF, "local_sign_in": "on", "default_role": "pending",
            "azure_client_secret": "TOPSECRET"})
        assert r.status_code == 400 and "TOPSECRET" not in r.text
    assert key.read_bytes() == b"too short"


def test_signin_section_renders_choice_as_select(client):
    r = client.get("/settings")
    assert "Sign-in" in r.text
    assert '<select class="input" id="f-default_role"' in r.text
    assert 'value="viewer" selected' in r.text
    assert "admin_legacy" not in r.text


def test_signin_save_refuses_turning_off_local_without_entra(client, app):
    # AZURE_* come from the env in the fixture, so Entra counts as configured
    r = client.post("/settings/signin", data={"csrf_token": CSRF, "default_role": "operator"})
    assert r.status_code == 303
    assert app.state.settings.default_role == "operator"
    assert app.state.settings.local_sign_in is False


def test_signin_refusal_shown_on_page(settings, tmp_path, monkeypatch):
    for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
        monkeypatch.delenv(k)
    from app.config import Settings

    store = Store(str(tmp_path / "x" / "dashboard.db"))
    app = create_app(Settings(_env_file=None), docker_client=object(), http=httpx.AsyncClient(),
                     store=store, runner=FakeRunner(), start_scheduler=False)
    try:
        with logged_in_client(app, follow_redirects=False) as c:
            r = c.post("/settings/signin", data={"csrf_token": CSRF, "default_role": "viewer"})
            assert r.status_code == 400
            assert "Turn on Microsoft sign-in first" in r.text
    finally:
        store.close()


def test_admin_legacy_option_only_while_current(client, app):
    assert "Admin (as in 0.1)" not in client.get("/settings").text
    app.state.store.save_settings({"default_role": "admin_legacy"})
    from app.main import apply_settings

    apply_settings(app)
    text = client.get("/settings").text
    assert '<option value="admin_legacy" selected>Admin (as in 0.1)</option>' in text
