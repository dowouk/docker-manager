import json

import httpx
import pytest

from app.config import Settings
from app.keys import SecretBox
from app.main import create_app
from app.notify.channels import (
    DEFAULT_EVENTS, EVENTS, Channel, ChannelError, ChannelStore,
)
from app.store import Store

SECRET = "endpoint=https://x;accesskey=TOPSECRET"
ACS = {"connection_string": SECRET, "sender": "from@x.com"}


def make(settings, tmp_path, key=b"k" * 32, store=None):
    store = store or Store(str(tmp_path / "d.db"))
    return ChannelStore(store, SecretBox(key), lambda: settings)


def new(name="Ops", provider="acs", recipients=("a@x.com",), events=("digest",), **kw):
    return Channel("", name, provider, {}, list(recipients), set(events), **kw)


def env_settings(monkeypatch):
    monkeypatch.setenv("ACS_CONNECTION_STRING", SECRET)
    monkeypatch.setenv("DIGEST_FROM", "env@x.com")
    monkeypatch.setenv("DIGEST_TO", "e1@x.com, e2@x.com")
    return Settings(_env_file=None)


def test_event_catalogue():
    assert EVENTS["digest"] == "Weekly digest" and EVENTS["user_pending"] == "User waiting for approval"
    assert DEFAULT_EVENTS == {"digest", "job_failed", "check_failing", "layout_problems", "user_pending"}


def test_env_channel_is_read_only_digest_only(settings, tmp_path, monkeypatch):
    cs = make(env_settings(monkeypatch), tmp_path)
    env = cs.list()[0]
    assert (env.id, env.name, env.provider) == ("env", "Email (from environment)", "acs")
    assert env.events == {"digest"} and env.readonly and env.recipients == ["e1@x.com", "e2@x.com"]
    assert cs.get("env") == env
    with pytest.raises(ChannelError):
        cs.save(env, {}, "me")
    with pytest.raises(ChannelError):
        cs.delete("env", "me")


def test_no_env_channel_when_partial(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ACS_CONNECTION_STRING", SECRET)
    assert make(Settings(_env_file=None), tmp_path).list() == []


def test_save_edit_delete(settings, tmp_path):
    cs = make(settings, tmp_path)
    ch = cs.save(new(recipients=["A@x.com", " a@X.com ", "b@x.com"]), ACS, "me")
    assert len(ch.id) == 16 and ch.recipients == ["A@x.com", "b@x.com"]
    assert cs.get(ch.id) == ch and cs.list() == [ch]
    edited = cs.save(Channel(ch.id, "Renamed", "acs", {}, ["c@x.com"], {"job_failed"}, False),
                     {"connection_string": "", "sender": "other@x.com"}, "me")
    assert edited.id == ch.id and edited.config == {"connection_string": SECRET, "sender": "other@x.com"}
    assert not edited.enabled and len(cs.list()) == 1
    cs.delete(ch.id, "me")
    assert cs.list() == []
    cs.delete(ch.id, "me")  # already gone: quiet


def test_validation_errors_per_field(settings, tmp_path):
    cs = make(settings, tmp_path)
    with pytest.raises(ChannelError) as exc:
        cs.save(new(name=" ", provider="acs", recipients=["nope"], events=["bogus"]), {}, "me")
    assert set(exc.value.errors) == {"name", "recipients", "events", "connection_string", "sender"}
    with pytest.raises(ChannelError) as exc:
        cs.save(new(name="x" * 61), ACS, "me")
    assert set(exc.value.errors) == {"name"}
    with pytest.raises(ChannelError) as exc:
        cs.save(new(provider="pigeon"), ACS, "me")
    assert "provider" in exc.value.errors
    with pytest.raises(ChannelError) as exc:
        cs.save(new(recipients=[]), ACS, "me")
    assert "recipients" in exc.value.errors
    with pytest.raises(ChannelError) as exc:
        cs.save(new(recipients=[f"u{i}@x.com" for i in range(21)]), ACS, "me")
    assert "recipients" in exc.value.errors
    assert cs.list() == []


def test_blank_secret_kept_only_for_same_provider(settings, tmp_path):
    cs = make(settings, tmp_path)
    ch = cs.save(new(), ACS, "me")
    assert cs.has_secret(ch.id, "connection_string") and not cs.has_secret(ch.id, "sender")
    with pytest.raises(ChannelError) as exc:
        cs.save(Channel(ch.id, "Ops", "smtp2go", {}, ["a@x.com"], {"digest"}),
                {"sender": "a@x.com", "api_key": ""}, "me")
    assert "api_key" in exc.value.errors
    assert cs.get(ch.id).provider == "acs"


def test_config_encrypted_and_audit_has_names_only(settings, tmp_path):
    cs = make(settings, tmp_path)
    ch = cs.save(new(name="Ops"), ACS, "me@x.com")
    cs.save(Channel(ch.id, "Ops2", "acs", {}, ["a@x.com"], {"digest"}),
            {"sender": "from@x.com"}, "me@x.com")
    cs.delete(ch.id, "me@x.com")
    raw = json.dumps(cs._store.load_settings())
    assert "TOPSECRET" not in raw and "from@x.com" not in raw
    summaries = [e.summary for e in cs._store.list_audit()]
    assert summaries == ["Notification channel removed: Ops2", "Notification channel changed: Ops2",
                         "Notification channel added: Ops"]
    assert all("TOPSECRET" not in x for x in summaries)


def test_subscribed_skips_disabled_and_unsubscribed(settings, tmp_path):
    cs = make(settings, tmp_path)
    a = cs.save(new("A", events=["job_failed", "digest"]), ACS, "me")
    cs.save(new("B", events=["job_failed"], enabled=False), ACS, "me")
    cs.save(new("C", events=["digest"]), ACS, "me")
    assert [c.id for c in cs.subscribed("job_failed")] == [a.id]
    assert len(cs.subscribed("digest")) == 2 and cs.subscribed("updates_found") == []


def test_provider_switch_drops_old_secret(settings, tmp_path):
    cs = make(settings, tmp_path)
    ch = cs.save(new(), ACS, "me")
    out = cs.save(Channel(ch.id, "Ops", "smtp2go", {}, ["a@x.com"], {"digest"}),
                  {"sender": "a@x.com", "api_key": "KEY"}, "me")
    assert "connection_string" not in out.config and out.config["api_key"] == "KEY"
    assert "connection_string" not in cs.get(ch.id).config


def test_unreadable_row_is_not_overwritten(settings, tmp_path):
    store = Store(str(tmp_path / "d.db"))
    make(settings, tmp_path, store=store).save(new(), ACS, "me")
    other = make(settings, tmp_path, key=b"z" * 32, store=store)
    assert other.list() == [] and other.unreadable
    with pytest.raises(ChannelError):
        other.save(new("Second"), ACS, "me")


def seed_0_2(store, box, **over):
    rows = {"acs_connection_string": {"enc": box.encrypt(SECRET)}, "digest_from": "d@x.com",
            "digest_to": ["x@x.com", "y@x.com"], "digest_schedule": "0 9 * * MON", **over}
    store.save_settings(rows)


def test_migration_creates_email_channel_once(settings, tmp_path):
    store = Store(str(tmp_path / "d.db"))
    box = SecretBox(b"k" * 32)
    seed_0_2(store, box)
    cs = ChannelStore(store, box, lambda: settings)
    cs.migrate_0_2_email()
    [ch] = cs.list()
    assert (ch.name, ch.provider, ch.events) == ("Email", "acs", {"digest"})
    assert ch.config == {"connection_string": SECRET, "sender": "d@x.com"}
    assert ch.recipients == ["x@x.com", "y@x.com"]
    rows = store.load_settings()
    assert not {"acs_connection_string", "digest_from", "digest_to"} & rows.keys()
    assert rows["digest_schedule"] == "0 9 * * MON"
    assert store.get_meta("email_channel_migrated")
    seed_0_2(store, box)  # a later stray row isn't migrated again
    cs.migrate_0_2_email()
    assert len(cs.list()) == 1
    assert all("TOPSECRET" not in e.summary for e in store.list_audit())
    assert store.list_audit()


def test_migration_env_wins(settings, tmp_path, monkeypatch):
    store = Store(str(tmp_path / "d.db"))
    box = SecretBox(b"k" * 32)
    seed_0_2(store, box)
    cs = ChannelStore(store, box, lambda: env_settings(monkeypatch))
    cs.migrate_0_2_email()
    assert [c.id for c in cs.list()] == ["env"]
    assert store.list_audit()[0].summary == "Email settings moved to notification channels"
    assert not {"acs_connection_string", "digest_from", "digest_to"} & store.load_settings().keys()


def test_migration_nothing_saved_and_unreadable(settings, tmp_path):
    store = Store(str(tmp_path / "d.db"))
    cs = ChannelStore(store, SecretBox(b"k" * 32), lambda: settings)
    cs.migrate_0_2_email()
    assert cs.list() == [] and store.get_meta("email_channel_migrated")
    store2 = Store(str(tmp_path / "e.db"))
    seed_0_2(store2, SecretBox(b"k" * 32))
    bad = ChannelStore(store2, SecretBox(b"z" * 32), lambda: settings)
    bad.migrate_0_2_email()
    assert "acs_connection_string" in store2.load_settings()  # kept for a later start
    assert not store2.get_meta("email_channel_migrated")


class FakeRunner:
    def submit(self, *args):
        raise AssertionError("not used")


def test_create_app_migrates_and_old_digest_still_sees_values(settings, tmp_path):
    store = Store(str(tmp_path / "d.db"))
    seed_0_2(store, SecretBox(b"k" * 32))
    (tmp_path / "secret.key").write_bytes(b"k" * 32)
    app = create_app(settings, docker_client=object(), http=httpx.AsyncClient(), store=store,
                     runner=FakeRunner(), start_scheduler=False)
    assert [c.name for c in app.state.channels.list()] == ["Email"]
    eff = app.state.settings
    assert eff.digest_enabled and eff.digest_to == ["x@x.com", "y@x.com"]
    assert eff.digest_schedule == "0 9 * * MON"


def test_migration_keeps_every_recipient(settings, tmp_path):
    store = Store(str(tmp_path / "d.db"))
    box = SecretBox(b"k" * 32)
    seed_0_2(store, box, digest_to=[f"u{i}@x.com" for i in range(25)])
    cs = ChannelStore(store, box, lambda: settings)
    cs.migrate_0_2_email()
    assert len(cs.list()[0].recipients) == 25


def test_migration_is_atomic(settings, tmp_path, monkeypatch):
    store = Store(str(tmp_path / "d.db"))
    box = SecretBox(b"k" * 32)
    seed_0_2(store, box)
    cs = ChannelStore(store, box, lambda: settings)
    monkeypatch.setattr(ChannelStore, "_audit", staticmethod(lambda *a: 1 / 0))
    with pytest.raises(ZeroDivisionError):
        cs.migrate_0_2_email()
    assert cs.list() == [] and "digest_from" in store.load_settings()
    assert not store.get_meta("email_channel_migrated")
    monkeypatch.undo()
    cs.migrate_0_2_email()
    assert len(cs.list()) == 1


def test_migration_does_not_overwrite_unreadable_channels(settings, tmp_path):
    store = Store(str(tmp_path / "d.db"))
    make(settings, tmp_path, store=store).save(new(), ACS, "me")
    seed_0_2(store, SecretBox(b"z" * 32))
    cs = ChannelStore(store, SecretBox(b"z" * 32), lambda: settings)
    before = store.load_settings()["notification_channels"]
    cs.migrate_0_2_email()
    assert store.load_settings()["notification_channels"] == before
    assert "digest_from" in store.load_settings() and not store.get_meta("email_channel_migrated")


def test_unreadable_warning_logged_once(settings, tmp_path, caplog):
    store = Store(str(tmp_path / "d.db"))
    make(settings, tmp_path, store=store).save(new(), ACS, "me")
    other = make(settings, tmp_path, key=b"z" * 32, store=store)
    other.list(); other.list(); other.subscribed("digest")
    assert caplog.text.count("can't be read") == 1


def test_create_app_survives_env_plus_saved_rows_with_bad_key(settings, tmp_path, monkeypatch):
    store = Store(str(tmp_path / "d.db"))
    store.save_settings({"digest_from": "d@x.com", "digest_to": ["x@x.com"]})
    monkeypatch.setenv("ACS_CONNECTION_STRING", SECRET)
    (tmp_path / "secret.key").write_bytes(b"too short")
    app = create_app(Settings(_env_file=None), docker_client=object(), http=httpx.AsyncClient(),
                     store=store, runner=FakeRunner(), start_scheduler=False)
    assert app.state.channels.list() == []
