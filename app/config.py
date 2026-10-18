from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    server_name: str = "Docker Manager"
    public_url: str
    azure_tenant_id: str
    azure_client_id: str
    azure_client_secret: str
    session_secret: str | None = None  # accepted so 0.1 .env files validate; unused
    allowed_group_id: str | None = None
    allowed_emails: Annotated[list[str], NoDecode] = []
    check_interval_hours: float = 6
    compose_root: str = "/docker"
    mailcow_dir: str = "/opt/mailcow-dockerized"
    acs_connection_string: str | None = None
    digest_from: str | None = None
    digest_to: Annotated[list[str], NoDecode] = []
    digest_schedule: str = "0 8 * * MON"
    digest_when_none: bool = True
    tz: str = "Europe/London"
    registry_auth_file: str | None = None
    health_timeout_seconds: float = 300
    data_dir: str = "/data"
    secret_key_file: str | None = None

    @field_validator("allowed_emails", "digest_to", mode="before")
    @classmethod
    def _split_commas(cls, value):
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def digest_enabled(self) -> bool:
        return bool(self.acs_connection_string and self.digest_from and self.digest_to)


@lru_cache
def get_settings() -> Settings:
    return Settings()
