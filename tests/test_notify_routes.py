"""Notification channels on the Settings page: forms, test button, role guard, secrets."""
import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

from app.config import Settings
from app.main import create_app
from app.notify import providers as prov
from app.notify.channels import Channel
from app.roles import Role
from app.store import Store
from auth_helpers import CSRF, logged_in_client
from notify_fakes import FakeProvider, failing, providers_for

SECRET = "SMTP2GO-TOPSECRET-KEY"
GOOD = {"csrf_token": CSRF, "name": "Ops mail", "recipients": "a@x.com, b@x.com",
        "events": ["digest", "job_failed"], "enabled": "on",
        "provider": "smtp2go", "api_key": SECRET, "sender": "dash@x.com"}
ENV_EMAIL = {"ACS_CONNECTION_STRING": "endpoint=https://x;accesskey=ENVSECRET",
             "DIGEST_FROM": "a@x.com", "DIGEST_TO": "b@x.com"}


class FakeRunner:
    def submit(self, *args):
        raise AssertionError("not used")


def build(settings, tmp_path, fake=None, **kw):
    return create_app(settings, docker_client=object(), http=httpx.AsyncClient(),
                      store=Store(str(tmp_path / "data" / "dashboard.db")),
                      runner=FakeRunner(), start_scheduler=False,
                      providers=providers_for(fake or FakeProvider()), **kw)


@pytest.fixture
def fake():
    return FakeProvider()


@pytest.fixture
def app(settings, tmp_path, fake):
    app = build(settings, tmp_path, fake)
    yield app
    app.state.store.close()


@pytest.fixture
def client(app):
    with logged_in_client(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def env_app(settings, tmp_path, monkeypatch, fake):
    for k, v in ENV_EMAIL.items():
        monkeypatch.setenv(k, v)
    app = build(Settings(_env_file=None), tmp_path, fake)
    yield app
    app.state.store.close()


def add(client, **over):
    data = {**GOOD, **over}
    return client.post("/settings/channels", data=data)


def only_channel(app) -> Channel:
    (channel,) = app.state.channels.list()
    return channel


# -- the page ---------------------------------------------------------------------------------
def test_page_has_notifications_card_and_no_old_test_button(client):
    r = client.get("/settings")
    assert 'id="notifications"' in r.text and "Add a channel" in r.text
    assert "Send test email" not in r.text and "/settings/email/test" not in r.text
    assert r.text.index('id="digest"') < r.text.index('id="notifications"')
    for key, p in prov.PROVIDERS.items():
        assert p.label in r.text


def test_add_form_per_provider(client):
    r = client.get("/settings/channels/new?provider=smtp")
    assert r.status_code == 200
    for name in ("name", "recipients", "host", "port", "security", "username", "password", "sender"):
        assert f'name="{name}"' in r.text
    assert 'name="events"' in r.text and 'value="starttls"' in r.text
    assert "data-preset" in r.text and "mail.smtp2go.com" in r.text
    r = client.get("/settings/channels/new?provider=smtp2go")
    assert 'name="api_key"' in r.text and "data-preset" not in r.text
    # new channels start with the default events ticked
    assert 'value="digest" checked' in r.text and 'value="job_succeeded" checked' not in r.text


@pytest.mark.parametrize("query", ["", "?provider=nope", "?provider=<b>x"])
def test_add_form_needs_a_known_provider(client, query):
    assert client.get(f"/settings/channels/new{query}").status_code == 404


@pytest.mark.parametrize("bad", ["xyz", "ABCDEF0123456789", "0123456789abcde", "0123456789abcdef0"])
def test_channel_id_is_validated(client, bad):
    assert client.get(f"/settings/channels/{bad}").status_code == 404
    assert client.post(f"/settings/channels/{bad}", data=GOOD).status_code == 404
    assert client.post(f"/settings/channels/{bad}/delete", data={"csrf_token": CSRF}).status_code == 404
    assert client.post(f"/settings/channels/{bad}/test", data={"csrf_token": CSRF}).status_code == 404


def test_unknown_but_well_formed_id_is_404(client):
    assert client.get("/settings/channels/0123456789abcdef").status_code == 404
    assert client.post("/settings/channels/0123456789abcdef/test",
                       data={"csrf_token": CSRF}).status_code == 404


# -- create / edit / delete -------------------------------------------------------------------
def test_create_edit_delete_round_trip(client, app):
    r = add(client)
    assert r.status_code == 303 and r.headers["location"] == "/settings?msg=channel_saved#notifications"
    ch = only_channel(app)
    assert ch.name == "Ops mail" and ch.recipients == ["a@x.com", "b@x.com"]
    assert ch.events == {"digest", "job_failed"} and ch.config["api_key"] == SECRET

    page = client.get("/settings").text
    assert "Ops mail" in page and "SMTP2GO" in page and "a@x.com" in page
    assert "Update failed" in page and "Not used yet" in page
    assert f"/settings/channels/{ch.id}" in page

    form = client.get(f"/settings/channels/{ch.id}").text
    assert 'value="Ops mail"' in form and "a@x.com, b@x.com" in form
    assert "Saved – enter a new value to replace it" in form

    r = client.post(f"/settings/channels/{ch.id}", data={
        **GOOD, "name": "Renamed", "api_key": "", "events": ["updates_found"]})
    assert r.status_code == 303
    ch2 = only_channel(app)
    assert ch2.id == ch.id and ch2.name == "Renamed" and ch2.events == {"updates_found"}
    assert ch2.config["api_key"] == SECRET  # blank keeps the saved secret

    r = client.post(f"/settings/channels/{ch.id}/delete", data={"csrf_token": CSRF})
    assert r.status_code == 303 and r.headers["location"] == "/settings?msg=channel_removed#notifications"
    assert app.state.channels.list() == []


def test_disabled_when_checkbox_absent(client, app):
    data = {k: v for k, v in GOOD.items() if k != "enabled"}
    client.post("/settings/channels", data=data)
    assert only_channel(app).enabled is False


def test_flash_messages_are_fixed_text(client):
    assert "Notification channel saved" in client.get("/settings?msg=channel_saved").text
    assert "Notification channel removed" in client.get("/settings?msg=channel_removed").text
    assert "<script>alert" not in client.get("/settings?msg=<script>alert(1)</script>").text


def test_validation_errors_rerender_400_with_values_and_no_secret(client, app):
    r = add(client, name="", recipients="not-an-address", sender="Oops mail <x>")
    assert r.status_code == 400
    assert "Enter a name" in r.text and "valid email addresses" in r.text
    assert "not-an-address" in r.text and "Oops mail &lt;x&gt;" in r.text
    assert SECRET not in r.text
    assert app.state.channels.list() == []


def test_smtp_errors_keep_submitted_non_secret_values(client):
    r = client.post("/settings/channels", data={
        **GOOD, "provider": "smtp", "host": "mail.example.com", "port": "99999",
        "security": "tls", "username": "bob", "password": "hunter2-secret", "sender": "d@x.com"})
    assert r.status_code == 400
    assert "Enter a number from 1 to 65535" in r.text
    assert 'value="mail.example.com"' in r.text and 'value="bob"' in r.text
    assert 'value="99999"' in r.text and "hunter2-secret" not in r.text
    assert '<option value="tls" selected>' in r.text


def test_edit_error_keeps_saved_secret_hint(client, app):
    add(client)
    ch = only_channel(app)
    r = client.post(f"/settings/channels/{ch.id}", data={**GOOD, "name": "", "api_key": ""})
    assert r.status_code == 400 and SECRET not in r.text
    assert "Saved – enter a new value to replace it" in r.text
    assert only_channel(app).name == "Ops mail"


def test_unknown_provider_and_events_rejected(client, app):
    assert add(client, provider="nope").status_code == 404
    assert add(client, events=["digest", "bogus"]).status_code == 400
    assert app.state.channels.list() == []


def test_secret_never_in_pages_or_history(client, app):
    add(client)
    ch = only_channel(app)
    for url in ("/settings", f"/settings/channels/{ch.id}", "/history?view=changes",
                "/settings?msg=channel_saved"):
        assert SECRET not in client.get(url).text
    client.post(f"/settings/channels/{ch.id}", data={**GOOD, "api_key": "NEW-TOPSECRET"})
    assert "NEW-TOPSECRET" not in client.get("/history?view=changes").text
    assert "Ops mail" in client.get("/history?view=changes").text


def test_csrf_refused(client, app):
    add(client)
    ch = only_channel(app)
    bad = {"X-CSRF-Token": "wrong"}
    assert client.post("/settings/channels", data={**GOOD, "csrf_token": ""},
                       headers=bad).status_code == 403
    assert client.post(f"/settings/channels/{ch.id}", data={}, headers=bad).status_code == 403
    assert client.post(f"/settings/channels/{ch.id}/delete", data={}, headers=bad).status_code == 403
    assert client.post(f"/settings/channels/{ch.id}/test", data={}, headers=bad).status_code == 403
    assert len(app.state.channels.list()) == 1


def test_operator_refused_everywhere(app):
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        assert c.get("/settings/channels/new?provider=smtp").status_code == 403
        assert c.post("/settings/channels", data=GOOD).status_code == 403
        assert c.post("/settings/channels/0123456789abcdef/test",
                      data={"csrf_token": CSRF}).status_code == 403
    assert app.state.channels.list() == []


# -- the environment channel ------------------------------------------------------------------
def test_env_channel_listed_read_only_but_testable(env_app, fake):
    with logged_in_client(env_app, follow_redirects=False) as c:
        page = c.get("/settings").text
        assert "Email (from environment)" in page
        assert "Set by the server's environment" in page
        assert "/settings/channels/env/test" in page
        assert "/settings/channels/env/delete" not in page
        assert 'href="/settings/channels/env"' not in page
        assert "ENVSECRET" not in page
        assert c.get("/settings/channels/env").status_code == 404
        assert c.post("/settings/channels/env", data=GOOD).status_code == 404
        assert c.post("/settings/channels/env/delete", data={"csrf_token": CSRF}).status_code == 404
        r = c.post("/settings/channels/env/test", data={"csrf_token": CSRF})
        assert r.status_code == 303
        assert fake.sent and fake.sent[0][1].recipients == ("b@x.com",)
        assert "Test sent to Email (from environment)" in c.get(r.headers["location"]).text
    assert len(env_app.state.channels.list()) == 1


# -- the test button --------------------------------------------------------------------------
def test_test_button_success(client, app, fake):
    add(client)
    ch = only_channel(app)
    r = client.post(f"/settings/channels/{ch.id}/test", data={"csrf_token": CSRF})
    assert r.status_code == 303 and r.headers["location"].endswith("#notifications")
    assert len(fake.sent) == 1
    page = client.get(r.headers["location"]).text
    assert "Test sent to Ops mail" in page
    assert "Last sent" in client.get("/settings").text


@pytest.mark.parametrize("reason", [
    prov.LOGIN_REFUSED, prov.UNREACHABLE, prov.REJECTED, prov.NO_ANSWER, prov.NOT_PRIVATE])
def test_test_button_failure_shows_name_and_fixed_reason(settings, tmp_path, reason):
    app = build(settings, tmp_path, failing(reason))
    with logged_in_client(app, follow_redirects=False) as c:
        add(c, name="<b>Ops</b>")
        ch = only_channel(app)
        r = c.post(f"/settings/channels/{ch.id}/test", data={"csrf_token": CSRF})
        page = c.get(r.headers["location"]).text
        assert f"be sent to &lt;b&gt;Ops&lt;/b&gt; – {reason.replace("'", "&#39;")}" in page
        assert "<b>Ops</b>" not in page
        assert f"Last attempt failed – {reason.replace("'", "&#39;")}" in c.get("/settings").text
    app.state.store.close()


def test_test_failure_never_shows_provider_text(settings, tmp_path, caplog):
    app = build(settings, tmp_path, FakeProvider(fail=RuntimeError("accesskey=TOPSECRET")))
    with logged_in_client(app, follow_redirects=True) as c:
        add(c)
        ch = only_channel(app)
        r = c.post(f"/settings/channels/{ch.id}/test", data={"csrf_token": CSRF})
        assert "TOPSECRET" not in r.text and prov.REJECTED in r.text
        assert "TOPSECRET" not in c.get("/settings").text
    assert "TOPSECRET" not in caplog.text
    app.state.store.close()


def test_reflected_query_text_is_never_shown(client, app):
    add(client)
    ch = only_channel(app)
    for url in (f"/settings?msg=channel_test_failed&channel={ch.id}&reason=<script>x</script>",
                f"/settings?msg=channel_test_failed&channel=<script>&reason=1",
                "/settings?msg=channel_test_failed&channel=zzzz&reason=0"):
        page = client.get(url).text
        assert "<script>x" not in page and "<script>&" not in page
        assert "couldn" not in page.split('id="notifications"')[0]


# -- the digest job follows the channels ------------------------------------------------------
def test_digest_job_follows_channels_without_restart(app, client):
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(app.state.settings.tz))
    app.state.scheduler = scheduler
    assert scheduler.get_job("digest") is None
    add(client, events=["job_failed"])
    assert scheduler.get_job("digest") is None
    add(client, name="Digest mail", events=["digest"])
    assert scheduler.get_job("digest") is not None
    ch = next(c for c in app.state.channels.list() if c.name == "Digest mail")
    client.post(f"/settings/channels/{ch.id}", data={**GOOD, "name": "Digest mail", "events": ["job_failed"]})
    assert scheduler.get_job("digest") is None
    client.post(f"/settings/channels/{ch.id}", data={**GOOD, "name": "Digest mail", "events": ["digest"]})
    assert scheduler.get_job("digest") is not None
    client.post(f"/settings/channels/{ch.id}/delete", data={"csrf_token": CSRF})
    assert scheduler.get_job("digest") is None


# -- phone width ------------------------------------------------------------------------------
def test_channels_are_a_stacked_list_not_a_table(client):
    add(client)
    block = client.get("/settings").text.split('id="notifications"')[1].split("</section>")[0]
    assert '<ul class="channel-list">' in block and "<table" not in block
    for text in ("Ops mail", "SMTP2GO", "a@x.com, b@x.com", "Weekly digest, Update failed",
                 "Not used yet", ">Edit<", ">Test<", ">Delete<"):
        assert text in block


def test_preset_block_served_hidden(client):
    page = client.get("/settings/channels/new?provider=smtp").text
    assert "data-presets hidden" in page


def test_digest_sync_failure_still_redirects(app, client, caplog, monkeypatch):
    app.state.scheduler = object()

    def boom(_app):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("app.scheduler.sync_digest_job", boom)
    r = add(client)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings?msg=channel_saved")
    assert len(app.state.channels.list()) == 1
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def test_microsoft_365_warning_shows_without_javascript(client):
    page = client.get("/settings/channels/new?provider=smtp").text
    warning = page.index("Microsoft 365 is ending username-and-password SMTP sign-in")
    start = page.index("data-presets")
    assert "</div>\n    <p" in page[start:warning]  # the hidden presets box has closed by then
    assert warning < page.index('name="host"')  # still next to the SMTP fields


def test_long_names_wrap_in_css():
    css = open("app/static/app.css").read()
    for selector in (".channel-head", ".who-name", ".channel-title"):
        rule = css[css.index(selector + " {"):].split("}")[0]
        assert "overflow-wrap: anywhere" in rule and "min-width: 0" in rule


def test_huge_reason_number_does_not_500(client, app):
    ch = app.state.channels.save(
        Channel("", "Ops", "smtp2go", {}, ["a@x.com"], {"digest"}),
        {"api_key": SECRET, "sender": "a@x.com"}, "me")
    r = client.get(f"/settings?msg=channel_test_failed&channel={ch.id}&reason={'9' * 5000}")
    assert r.status_code == 200 and "couldn't be sent" not in r.text


# -- unreadable channels ----------------------------------------------------------------------
def make_unreadable(app):
    add_ok = app.state.channels.save(
        Channel("", "Ops", "smtp2go", {}, ["a@x.com"], {"digest"}),
        {"api_key": SECRET, "sender": "a@x.com"}, "me")
    with app.state.store.write_tx() as db:
        db.execute("UPDATE settings SET value = ? WHERE name = 'notification_channels'",
                   ('{"enc": "not-a-valid-token"}',))
    return add_ok


def reset(client, **over):
    return client.post("/settings/channels/reset-unreadable",
                       data={"csrf_token": CSRF, "confirm": "1", **over})


def test_unreadable_banner_explains_and_offers_the_reset(client, app):
    make_unreadable(app)
    page = client.get("/settings").text
    assert ("The saved notification channels can't be read with the current key. Restore the "
            "original key file, or remove them and add them again.") in page
    assert 'action="/settings/channels/reset-unreadable"' in page and 'name="confirm"' in page


def test_reset_removes_only_an_unreadable_row_and_is_audited(client, app):
    make_unreadable(app)
    r = reset(client)
    assert r.status_code == 303 and "msg=channel_reset" in r.headers["location"]
    assert app.state.channels.list() == [] and not app.state.channels.unreadable
    assert any("Unreadable notification channels removed" in e.summary
               for e in app.state.store.list_audit())
    assert add(client).status_code == 303 and len(app.state.channels.list()) == 1


def test_reset_needs_confirmation(client, app):
    make_unreadable(app)
    r = client.post("/settings/channels/reset-unreadable", data={"csrf_token": CSRF})
    assert "channel_reset_refused" in r.headers["location"]
    assert client.get("/settings").text.count("reset-unreadable") >= 1


def test_reset_never_deletes_readable_channels(client, app):
    add(client)
    r = reset(client)
    assert "channel_reset_refused" in r.headers["location"]
    assert len(app.state.channels.list()) == 1


def test_reset_needs_csrf_and_admin(app):
    make_unreadable(app)
    with logged_in_client(app, follow_redirects=False) as c:
        assert c.post("/settings/channels/reset-unreadable",
                      data={"confirm": "1", "csrf_token": "wrong"}).status_code in (400, 403)
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        assert reset(c).status_code == 403
    assert app.state.channels.unreadable or app.state.channels.list() == []
    with app.state.store.write_tx() as db:
        assert db.execute("SELECT 1 FROM settings WHERE name='notification_channels'").fetchone()
