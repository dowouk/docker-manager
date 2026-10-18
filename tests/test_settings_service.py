import json

import pytest

from app.config import Settings
from app.keys import SecretBox
from app.settings_service import SettingsError, SettingsService, key_path
from app.store import Store


def svc(settings, tmp_path, key=b"k" * 32):
    return SettingsService(settings, Store(str(tmp_path / "d.db")), SecretBox(key))


def test_db_value_used_when_env_not_set(settings, tmp_path):
    s = svc(settings, tmp_path)
    s.save("checking", {"check_interval_hours": "3", "health_timeout_seconds": "300",
                        "mailcow_dir": "/opt/mailcow-dockerized"}, "me@x.com")
    assert s.effective().check_interval_hours == 3


def test_env_wins_and_locked_field_ignored(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("CHECK_INTERVAL_HOURS", "12")
    base = Settings(_env_file=None)
    s = SettingsService(base, Store(str(tmp_path / "d.db")), SecretBox(b"k" * 32))
    assert s.locked("check_interval_hours") == "CHECK_INTERVAL_HOURS"
    s.save("checking", {"check_interval_hours": "1", "health_timeout_seconds": "300",
                        "mailcow_dir": "/opt/mailcow-dockerized"}, "me@x.com")
    assert s.effective().check_interval_hours == 12
    assert "check_interval_hours" not in s._store.load_settings()


def test_invalid_value_saves_nothing(settings, tmp_path):
    s = svc(settings, tmp_path)
    with pytest.raises(SettingsError) as exc:
        s.save("email", {"digest_from": "a@x.com", "digest_to": "b@x.com",
                         "digest_schedule": "not cron", "digest_when_none": "on"}, "me")
    assert "digest_schedule" in exc.value.errors
    assert s._store.load_settings() == {}


@pytest.mark.parametrize("field,value", [("tz", "Mars/Olympus"), ("server_name", "  ")])
def test_general_validation(settings, tmp_path, field, value):
    form = {"server_name": "web1", "tz": "Europe/London", field: value}
    with pytest.raises(SettingsError):
        svc(settings, tmp_path).save("general", form, "me")


def test_interval_must_be_positive(settings, tmp_path):
    with pytest.raises(SettingsError):
        svc(settings, tmp_path).save("checking", {"check_interval_hours": "0",
            "health_timeout_seconds": "300", "mailcow_dir": "/opt/m"}, "me")


def test_secret_encrypted_blank_keeps_and_never_returned(settings, tmp_path):
    s = svc(settings, tmp_path)
    form = {"acs_connection_string": "endpoint=https://x;accesskey=SECRET", "digest_from": "a@x.com",
            "digest_to": "b@x.com, c@x.com", "digest_schedule": "0 8 * * MON", "digest_when_none": "on"}
    s.save("email", form, "me")
    assert "SECRET" not in json.dumps(s._store.load_settings())
    s.save("email", {**form, "acs_connection_string": ""}, "me")
    assert s.effective().acs_connection_string.endswith("SECRET")
    assert s.form_values("email")["acs_connection_string"] == ""
    assert s.has_secret("acs_connection_string")
    assert s.effective().digest_to == ["b@x.com", "c@x.com"]


def test_unreadable_secret_is_unset_not_crash(settings, tmp_path):
    store = Store(str(tmp_path / "d.db"))
    SettingsService(settings, store, SecretBox(b"k" * 32)).save("email", {
        "acs_connection_string": "endpoint=x", "digest_from": "a@x.com", "digest_to": "b@x.com",
        "digest_schedule": "0 8 * * MON", "digest_when_none": "on"}, "me")
    other = SettingsService(settings, store, SecretBox(b"z" * 32))
    assert other.effective().acs_connection_string is None
    assert "acs_connection_string" in other.unreadable


def test_audit_records_change_without_secret_value(settings, tmp_path):
    s = svc(settings, tmp_path)
    s.save("email", {"acs_connection_string": "endpoint=SECRET", "digest_from": "a@x.com",
                     "digest_to": "b@x.com", "digest_schedule": "0 8 * * MON",
                     "digest_when_none": ""}, "me@x.com")
    s.save("general", {"server_name": "ignored", "tz": "Europe/Paris"}, "me@x.com")  # SERVER_NAME is env-locked
    entries = s._store.list_audit()
    assert entries[0].summary == "General: Timezone Europe/London → Europe/Paris"
    assert entries[0].user == "me@x.com"
    assert all("SECRET" not in e.summary for e in entries)
    assert any("Connection string replaced" in e.summary for e in entries)


def test_no_change_no_audit(settings, tmp_path):
    s = svc(settings, tmp_path)
    form = {"check_interval_hours": "6", "health_timeout_seconds": "300", "mailcow_dir": "/opt/mailcow-dockerized"}
    assert s.save("checking", form, "me") == []
    assert s._store.list_audit() == []


def test_registry_logins_round_trip_encrypted(settings, tmp_path):
    s = svc(settings, tmp_path)
    assert s.registry_logins() == []
    s.save_registry_logins([{"host": "ghcr.io", "password": "PW"}], "me", "Registries: added ghcr.io")
    assert s.registry_logins() == [{"host": "ghcr.io", "password": "PW"}]
    assert "PW" not in json.dumps(s._store.load_settings())
    assert s._store.list_audit()[0].summary == "Registries: added ghcr.io"
    other = svc(settings, tmp_path, key=b"z" * 32)
    assert other.registry_logins() == []


def test_key_path_defaults_to_data_dir(settings, tmp_path):
    assert key_path(settings) == str(tmp_path / "secret.key")
