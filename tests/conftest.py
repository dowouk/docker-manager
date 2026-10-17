import pytest

from app.config import Settings, get_settings


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    for key, value in {
        "SERVER_NAME": "test-server",
        "PUBLIC_URL": "https://dash.example.com",
        "AZURE_TENANT_ID": "tenant",
        "AZURE_CLIENT_ID": "client",
        "AZURE_CLIENT_SECRET": "secret",
        "SESSION_SECRET": "session-secret",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "ALLOWED_GROUP_ID", "ALLOWED_EMAILS", "ACS_CONNECTION_STRING",
        "DIGEST_FROM", "DIGEST_TO", "REGISTRY_AUTH_FILE", "HEALTH_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    return Settings(_env_file=None)
