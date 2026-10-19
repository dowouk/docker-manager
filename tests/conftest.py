import pytest

from app import photos
from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def no_photo_lookups(monkeypatch):
    # Background Gravatar lookups would reach the network; tests call them directly instead.
    monkeypatch.setattr(photos, "LOOKUPS_ENABLED", False)


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
        "SECRET_KEY_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    return Settings(_env_file=None)


@pytest.fixture(autouse=True)
def _no_docker_config_dir():
    """apply_settings sets a process-wide config dir; don't let it leak between tests."""
    from app.jobs import set_docker_config_dir

    set_docker_config_dir(None)
    yield
    set_docker_config_dir(None)


@pytest.fixture(autouse=True)
def _existing_install(request, monkeypatch):
    """Most tests mean "a dashboard that is already set up": mark the database as an
    upgraded install (setup closed, no migration) unless the test is about first start."""
    if request.node.get_closest_marker("fresh_install"):
        return
    from app.setup import Setup

    real = Setup.prepare

    def prepare(self, settings):
        self._store.set_meta("existing_install", "1")
        self._store.set_meta("allow_rules_migrated", "1")
        real(self, settings)

    monkeypatch.setattr(Setup, "prepare", prepare)
