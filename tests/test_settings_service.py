import json

import pytest

from app.config import Settings
from app.keys import SecretBox
from app.settings_service import SettingsError, SettingsService, key_path
from app.store import Store


TENANT = "33333333-3333-3333-3333-333333333333"


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
        s.save("digest", {"digest_schedule": "not cron", "digest_when_none": "on"}, "me")
    assert "digest_schedule" in exc.value.errors
    assert s._store.load_settings() == {}


@pytest.mark.parametrize("field,value", [("tz", "Mars/Olympus"), ("server_name", "  ")])
def test_general_validation(settings, tmp_path, monkeypatch, field, value):
    monkeypatch.delenv("SERVER_NAME")
    base = Settings(_env_file=None)
    form = {"server_name": "web1", "tz": "Europe/London", field: value}
    with pytest.raises(SettingsError):
        svc(base, tmp_path).save("general", form, "me")


def test_locked_field_with_invalid_value_is_ignored(settings, tmp_path):
    s = svc(settings, tmp_path)  # SERVER_NAME is env-locked
    assert s.save("general", {"server_name": "  ", "tz": "Europe/London"}, "me") == []
    assert s._store.load_settings() == {}


def test_unknown_section_raises(settings, tmp_path):
    with pytest.raises(ValueError):
        svc(settings, tmp_path).save("nope", {}, "me")


def test_interval_must_be_positive(settings, tmp_path):
    with pytest.raises(SettingsError):
        svc(settings, tmp_path).save("checking", {"check_interval_hours": "0",
            "health_timeout_seconds": "300", "mailcow_dir": "/opt/m"}, "me")


SIGNIN = {"local_sign_in": "on", "default_role": "pending", "azure_client_secret": "endpoint=SECRET"}


@pytest.fixture
def open_settings(settings, monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_SECRET")
    return Settings(_env_file=None)


def test_secret_encrypted_blank_keeps_and_never_returned(open_settings, tmp_path):
    s = svc(open_settings, tmp_path)
    s.save("signin", SIGNIN, "me")
    assert "SECRET" not in json.dumps(s._store.load_settings())
    s.save("signin", {**SIGNIN, "azure_client_secret": ""}, "me")
    assert s.effective().azure_client_secret.endswith("SECRET")
    assert s.form_values("signin")["azure_client_secret"] == ""
    assert s.has_secret("azure_client_secret")


def test_unreadable_secret_is_unset_not_crash(open_settings, tmp_path):
    settings = open_settings
    store = Store(str(tmp_path / "d.db"))
    SettingsService(settings, store, SecretBox(b"k" * 32)).save("signin", SIGNIN, "me")
    other = SettingsService(settings, store, SecretBox(b"z" * 32))
    assert other.effective().azure_client_secret is None
    assert "azure_client_secret" in other.unreadable


def test_audit_records_change_without_secret_value(open_settings, tmp_path):
    s = svc(open_settings, tmp_path)
    s.save("signin", SIGNIN, "me@x.com")
    s.save("general", {"server_name": "ignored", "tz": "Europe/Paris"}, "me@x.com")  # SERVER_NAME is env-locked
    entries = s._store.list_audit()
    assert entries[0].summary == "General: Timezone Europe/London → Europe/Paris"
    assert entries[0].user == "me@x.com"
    assert all("SECRET" not in e.summary for e in entries)
    assert any("Client secret replaced" in e.summary for e in entries)


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


def test_empty_env_value_does_not_lock(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ACS_CONNECTION_STRING", "")
    monkeypatch.setenv("DIGEST_FROM", "")
    monkeypatch.setenv("DIGEST_TO", "")
    base = Settings(_env_file=None)
    s = svc(base, tmp_path)
    for name in ("acs_connection_string", "digest_from", "digest_to"):
        assert s.locked(name) is None


def test_false_env_value_still_locks(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_WHEN_NONE", "false")
    s = svc(Settings(_env_file=None), tmp_path)
    assert s.locked("digest_when_none") == "DIGEST_WHEN_NONE"
    s.save("digest", {"digest_when_none": "on", "digest_schedule": "0 8 * * MON"}, "me")
    assert s.effective().digest_when_none is False


def test_locked_secret_field_in_form_is_skipped(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ACS_CONNECTION_STRING", "endpoint=from-env")
    s = svc(Settings(_env_file=None), tmp_path)
    assert s.locked("acs_connection_string") == "ACS_CONNECTION_STRING"
    assert s.effective().acs_connection_string == "endpoint=from-env"


GUID = "22222222-2222-2222-2222-222222222222"


def _unlocked(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(n, raising=False)
    return Settings(_env_file=None)


GENERAL = {"server_name": "web1", "tz": "Europe/London"}


@pytest.mark.parametrize("url,ok", [
    ("https://dash.example.com", True), ("https://dash.example.com/", True),
    ("http://localhost:8080", True), ("http://127.0.0.1:8000", True),
    ("http://example.com", False), ("https://x/path", False), ("", True),
])
def test_public_url_validation(settings, tmp_path, monkeypatch, url, ok):
    base = _unlocked(monkeypatch, "SERVER_NAME", "PUBLIC_URL")
    s = svc(base, tmp_path)
    form = {**GENERAL, "public_url": url}
    if ok:
        s.save("general", form, "me")
    else:
        with pytest.raises(SettingsError) as exc:
            s.save("general", form, "me")
        assert "public_url" in exc.value.errors


def test_choice_rejects_admin_and_legacy_only_when_current(settings, tmp_path, monkeypatch):
    base = _unlocked(monkeypatch, "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    s = svc(base, tmp_path)
    for bad in ("admin", "admin_legacy"):
        with pytest.raises(SettingsError) as exc:
            s.save("signin", {"local_sign_in": "on", "default_role": bad}, "me")
        assert "default_role" in exc.value.errors
    s._store.save_settings({"default_role": "admin_legacy"})
    s.save("signin", {"local_sign_in": "on", "default_role": "admin_legacy"}, "me")
    assert s.effective().default_role == "admin_legacy"
    s.save("signin", {"local_sign_in": "on", "default_role": "operator"}, "me")
    with pytest.raises(SettingsError):
        s.save("signin", {"local_sign_in": "on", "default_role": "admin_legacy"}, "me")


def test_cannot_turn_off_local_without_entra(settings, tmp_path, monkeypatch):
    base = _unlocked(monkeypatch, "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    s = svc(base, tmp_path)
    with pytest.raises(SettingsError) as exc:
        s.save("signin", {"default_role": "viewer"}, "me")  # local_sign_in unticked
    assert "Turn on Microsoft sign-in first" in exc.value.errors["local_sign_in"]
    assert s._store.load_settings() == {}


def test_can_turn_off_local_with_entra_configured(settings, tmp_path):
    s = svc(settings, tmp_path)  # AZURE_* from env
    s.save("signin", {"default_role": "viewer"}, "me")
    assert s.effective().local_sign_in is False


def test_can_turn_off_local_when_entra_saved_in_same_form(settings, tmp_path, monkeypatch):
    base = _unlocked(monkeypatch, "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    s = svc(base, tmp_path)
    s.save("signin", {"default_role": "viewer", "azure_tenant_id": TENANT,
                      "azure_client_id": "c", "azure_client_secret": "s"}, "me")
    assert s.effective().entra_enabled and not s.effective().local_sign_in


def test_env_locks_azure_fields(settings, tmp_path):
    s = svc(settings, tmp_path)
    assert s.locked("azure_tenant_id") == "AZURE_TENANT_ID"
    s.save("signin", {"local_sign_in": "on", "default_role": "viewer",
                      "azure_tenant_id": "other"}, "me")
    assert s.effective().azure_tenant_id == "tenant"


def test_azure_secret_never_audited(settings, tmp_path, monkeypatch):
    base = _unlocked(monkeypatch, "AZURE_CLIENT_SECRET")
    s = svc(base, tmp_path)
    s.save("signin", {"local_sign_in": "on", "default_role": "viewer",
                      "azure_client_secret": "hunter2"}, "me")
    assert "hunter2" not in str(s._store.list_audit())
    assert s.effective().azure_client_secret == "hunter2"


def test_group_mappings_roundtrip_and_audit(settings, tmp_path):
    s = svc(settings, tmp_path)
    assert s.group_mappings() == []
    s.save_group_mappings([{"group_id": GUID, "role": "admin"}], "me",
                          f"Group mapping {GUID} → Admin added")
    assert s.group_mappings() == [{"group_id": GUID, "role": "admin"}]
    assert any(f"Group mapping {GUID} → Admin added" in str(a) for a in s._store.list_audit())


@pytest.mark.parametrize("bad", [
    [{"group_id": "nope", "role": "admin"}],
    [{"group_id": GUID, "role": "pending"}],
    [{"group_id": GUID, "role": "root"}],
    [{"group_id": GUID}],
])
def test_group_mappings_validated(settings, tmp_path, bad):
    s = svc(settings, tmp_path)
    with pytest.raises(ValueError):
        s.save_group_mappings(bad, "me", "x")
    assert s.group_mappings() == []


def test_env_default_role_and_local_sign_in_ignored(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("DEFAULT_ROLE", "admin")
    monkeypatch.setenv("LOCAL_SIGN_IN", "false")
    s = svc(Settings(_env_file=None), tmp_path)
    assert s.locked("default_role") is None and s.locked("local_sign_in") is None
    eff = s.effective()
    assert eff.default_role == "viewer" and eff.local_sign_in is True


def test_db_local_off_without_entra_is_forced_on(settings, tmp_path, monkeypatch):
    base = _unlocked(monkeypatch, "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    s = svc(base, tmp_path)
    s._store.save_settings({"local_sign_in": False})
    assert s.effective().local_sign_in is True


@pytest.mark.parametrize("url", ["https://u:p@x.example", "https://x.example/?a=1",
                                 "https://x.example/#f", "https://x.example@evil.com"])
def test_public_url_rejects_credentials_query_fragment(settings, tmp_path, monkeypatch, url):
    base = _unlocked(monkeypatch, "SERVER_NAME", "PUBLIC_URL")
    with pytest.raises(SettingsError):
        svc(base, tmp_path).save("general", {**GENERAL, "public_url": url}, "me")


def test_duplicate_group_ids_rejected(settings, tmp_path):
    s = svc(settings, tmp_path)
    with pytest.raises(ValueError):
        s.save_group_mappings([{"group_id": GUID, "role": "admin"},
                               {"group_id": GUID.upper(), "role": "viewer"}], "me", "x")


def test_tenant_id_must_be_a_guid(settings, tmp_path, monkeypatch):
    base = _unlocked(monkeypatch, "AZURE_TENANT_ID")
    s = svc(base, tmp_path)
    with pytest.raises(SettingsError) as exc:
        s.save("signin", {"local_sign_in": "on", "default_role": "viewer",
                          "azure_tenant_id": "contoso.onmicrosoft.com"}, "me")
    assert exc.value.errors["azure_tenant_id"] == (
        "Use the Directory (tenant) ID – the long code, not your domain name")
    s.save("signin", {"local_sign_in": "on", "default_role": "viewer",
                      "azure_tenant_id": TENANT}, "me")
    assert s.effective().azure_tenant_id == TENANT
