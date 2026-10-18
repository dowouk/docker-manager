from app.config import Settings


def test_defaults(settings):
    assert settings.check_interval_hours == 6
    assert settings.compose_root == "/docker"
    assert settings.mailcow_dir == "/opt/mailcow-dockerized"
    assert settings.digest_schedule == "0 8 * * MON"
    assert settings.tz == "Europe/London"


def test_comma_lists(monkeypatch, settings):
    monkeypatch.setenv("DIGEST_TO", "a@x.com, b@x.com")
    monkeypatch.setenv("ALLOWED_EMAILS", "me@x.com")
    s = Settings(_env_file=None)
    assert s.digest_to == ["a@x.com", "b@x.com"]
    assert s.allowed_emails == ["me@x.com"]


def test_nothing_is_required(monkeypatch):
    for k in ("AZURE_CLIENT_ID", "PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    assert not Settings(_env_file=None).entra_enabled


def test_session_secret_optional(monkeypatch, settings):
    monkeypatch.delenv("SESSION_SECRET")
    assert Settings(_env_file=None).session_secret is None


def test_server_name_default(monkeypatch, settings):
    monkeypatch.delenv("SERVER_NAME")
    assert Settings(_env_file=None).server_name == "Docker Manager"


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


def test_entra_optional_and_enabled_property(monkeypatch):
    from app.config import Settings

    for k in ("PUBLIC_URL", "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.public_url is None and not s.entra_enabled
    assert s.local_sign_in and s.require_totp_admins and not s.hide_local_for_non_admins
    assert s.default_role == "viewer"
    s = Settings(_env_file=None, public_url="https://x.example", azure_tenant_id="t",
                 azure_client_id="c", azure_client_secret="s")
    assert s.entra_enabled


def test_entra_needs_public_url(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("PUBLIC_URL", raising=False)
    s = Settings(_env_file=None, azure_tenant_id="t", azure_client_id="c", azure_client_secret="s")
    assert not s.entra_enabled
    assert Settings(_env_file=None, public_url="https://x.example", azure_tenant_id="t",
                    azure_client_id="c", azure_client_secret="s").entra_enabled


def test_bad_default_role_falls_back(monkeypatch):
    from app.config import Settings

    assert Settings(_env_file=None, default_role="admin").default_role == "viewer"
    assert Settings(_env_file=None, default_role="admin_legacy").default_role == "admin_legacy"
