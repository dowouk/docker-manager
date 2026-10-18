"""Settings page, test buttons and the History "Changes" view."""
import httpx
import pytest

from app.main import create_app
from app.store import Store
from auth_helpers import CSRF, assert_all_routes_guarded, logged_in_client

SECRET_STRING = "endpoint=https://x;accesskey=TOPSECRET"
GOOD_EMAIL = {
    "csrf_token": CSRF, "acs_connection_string": SECRET_STRING,
    "digest_from": "a@x.com", "digest_to": "b@x.com", "digest_schedule": "0 8 * * MON",
}


class FakeRunner:
    def submit(self, *args):
        raise AssertionError("not used")


@pytest.fixture
def app(settings, tmp_path):
    return create_app(
        settings,
        docker_client=object(),
        http=httpx.AsyncClient(),
        store=Store(str(tmp_path / "data" / "dashboard.db")),
        runner=FakeRunner(),
        start_scheduler=False,
    )


@pytest.fixture
def client(app):
    with logged_in_client(app, follow_redirects=False) as c:
        yield c


def test_settings_page_lists_sections_and_locks_env(client):  # SERVER_NAME set in fixture env
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Set by the server's environment (<code>SERVER_NAME</code>)" in r.text
    for heading in ("General", "Checking", "Email", "Registry logins"):
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
    r = client.post("/settings/email", data={
        "csrf_token": CSRF, "digest_from": "a@x.com", "digest_to": "b@x.com",
        "digest_schedule": "every monday", "digest_when_none": "on"})
    assert r.status_code == 400 and "every monday" in r.text and "a@x.com" in r.text
    assert "five-part cron schedule" in r.text


def test_secret_never_rendered(client):
    client.post("/settings/email", data=GOOD_EMAIL)
    for url in ("/settings", "/history?view=changes"):
        assert "TOPSECRET" not in client.get(url).text
    assert "Saved – enter a new value to replace it" in client.get("/settings").text


def test_secret_not_echoed_on_error(client):
    r = client.post("/settings/email", data={**GOOD_EMAIL, "digest_schedule": "nope"})
    assert r.status_code == 400 and "TOPSECRET" not in r.text


def test_unknown_section_404(client):
    assert client.post("/settings/nope", data={"csrf_token": CSRF}).status_code == 404


def test_email_test_unconfigured(client):
    r = client.post("/settings/email/test", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/settings?msg=email_unconfigured#email"


def test_email_test_sent(settings, tmp_path):
    sent = []

    class Poller:
        def result(self):
            return None

    class Client:
        def begin_send(self, message):
            sent.append(message)
            return Poller()

    app = create_app(settings, docker_client=object(), http=httpx.AsyncClient(),
                     store=Store(str(tmp_path / "d.db")), runner=FakeRunner(),
                     start_scheduler=False, email_client_factory=lambda conn: Client())
    with logged_in_client(app, follow_redirects=False) as c:
        c.post("/settings/email", data=GOOD_EMAIL)
        r = c.post("/settings/email/test", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/settings?msg=email_sent#email"
    assert sent and sent[0]["recipients"]["to"] == [{"address": "b@x.com"}]


def test_email_test_failure_hides_error_text(settings, tmp_path):
    def factory(conn):
        raise RuntimeError("accesskey=TOPSECRET")

    app = create_app(settings, docker_client=object(), http=httpx.AsyncClient(),
                     store=Store(str(tmp_path / "d.db")), runner=FakeRunner(),
                     start_scheduler=False, email_client_factory=factory)
    with logged_in_client(app, follow_redirects=True) as c:
        c.post("/settings/email", data=GOOD_EMAIL)
        r = c.post("/settings/email/test", data={"csrf_token": CSRF})
    assert "TOPSECRET" not in r.text and "The test email could not be sent" in r.text


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


def test_all_routes_still_guarded(app):
    assert_all_routes_guarded(app)
