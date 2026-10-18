import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults(settings):
    assert settings.check_interval_hours == 6
    assert settings.compose_root == "/docker"
    assert settings.mailcow_dir == "/opt/mailcow-dockerized"
    assert settings.digest_schedule == "0 8 * * MON"
    assert settings.tz == "Europe/London"
    assert settings.digest_enabled is False


def test_comma_lists(monkeypatch, settings):
    monkeypatch.setenv("DIGEST_TO", "a@x.com, b@x.com")
    monkeypatch.setenv("ALLOWED_EMAILS", "me@x.com")
    s = Settings(_env_file=None)
    assert s.digest_to == ["a@x.com", "b@x.com"]
    assert s.allowed_emails == ["me@x.com"]


def test_missing_required_fails(monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_session_secret_optional(monkeypatch, settings):
    monkeypatch.delenv("SESSION_SECRET")
    assert Settings(_env_file=None).session_secret is None


def test_server_name_default(monkeypatch, settings):
    monkeypatch.delenv("SERVER_NAME")
    assert Settings(_env_file=None).server_name == "Docker Manager"


def test_digest_enabled_needs_all_three(monkeypatch, settings):
    monkeypatch.setenv("ACS_CONNECTION_STRING", "endpoint=x")
    monkeypatch.setenv("DIGEST_FROM", "d@x.com")
    assert Settings(_env_file=None).digest_enabled is False
    monkeypatch.setenv("DIGEST_TO", "a@x.com")
    assert Settings(_env_file=None).digest_enabled is True


def test_registry_auth_file_not_docker_config(monkeypatch, settings):
    # DOCKER_CONFIG is a directory to the docker CLI; the dashboard must not claim it
    monkeypatch.setenv("DOCKER_CONFIG", "/root/.docker")
    monkeypatch.setenv("REGISTRY_AUTH_FILE", "/auth/config.json")
    s = Settings(_env_file=None)
    assert s.registry_auth_file == "/auth/config.json"
    assert not hasattr(s, "docker_config")


def test_health_timeout_setting(monkeypatch, settings):
    assert settings.health_timeout_seconds == 300
    monkeypatch.setenv("HEALTH_TIMEOUT_SECONDS", "90")
    assert Settings(_env_file=None).health_timeout_seconds == 90
