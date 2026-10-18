"""Settings kept in the database, layered under environment variables."""
import json
import logging
import os
from dataclasses import dataclass
from typing import Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.keys import SecretBox
from app.store import Store

log = logging.getLogger(__name__)

REGISTRY_LOGINS = "registry_logins"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    section: str
    kind: Literal["text", "int", "float", "bool", "email", "email_list",
                  "cron", "tz", "path", "secret"]
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None


SECTIONS = {"general": "General", "checking": "Checking", "email": "Email"}

FIELDS: list[Field] = [
    Field("server_name", "Server name", "general", "text",
          "Shown at the top of the dashboard and in email subjects."),
    Field("tz", "Timezone", "general", "tz",
          "Used for the weekly digest schedule, for example Europe/London."),
    Field("check_interval_hours", "Check every (hours)", "checking", "float",
          "How often the dashboard looks for newer images.", 0.25, 168),
    Field("health_timeout_seconds", "Health check timeout (seconds)", "checking", "float",
          "How long to wait for a container to become healthy after an update.", 30, 3600),
    Field("mailcow_dir", "Mailcow folder", "checking", "path",
          "Full path to the Mailcow folder on this server."),
    Field("acs_connection_string", "Connection string", "email", "secret",
          "From your Azure Communication Services resource. Leave blank to keep the saved one."),
    Field("digest_from", "Sender address", "email", "email",
          "The address the digest is sent from."),
    Field("digest_to", "Recipients", "email", "email_list",
          "Separate several addresses with commas."),
    Field("digest_schedule", "Weekly digest schedule", "email", "cron",
          "Five-part cron schedule, for example 0 8 * * MON for Mondays at 8am."),
    Field("digest_when_none", "Send the digest when everything is up to date", "email", "bool",
          "Tick to get an email even when there are no updates."),
]
_BY_NAME = {f.name: f for f in FIELDS}


class SettingsError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def key_path(settings: Settings) -> str:
    return settings.secret_key_file or os.path.join(settings.data_dir, "secret.key")


def _valid_email(value: str) -> bool:
    local, at, domain = value.partition("@")
    return bool(at and local and domain and "@" not in domain and not any(c.isspace() for c in value))


def _parse(field: Field, raw: str):
    """Turn a form string into a stored value; raise ValueError with a user-facing message."""
    raw = (raw or "").strip()
    kind = field.kind
    if kind == "bool":
        return raw == "on"
    if kind == "text":
        if not raw:
            raise ValueError("Enter a value")
        return raw
    if kind in ("int", "float"):
        try:
            number = float(raw)
        except ValueError:
            raise ValueError("Enter a number") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("Enter a number")
        if field.minimum is not None and number < field.minimum:
            raise ValueError(f"Must be at least {field.minimum:g}")
        if field.maximum is not None and number > field.maximum:
            raise ValueError(f"Must be at most {field.maximum:g}")
        return int(number) if kind == "int" else number
    if kind == "email":
        if not raw:
            return None
        if not _valid_email(raw):
            raise ValueError("Enter a valid email address")
        return raw
    if kind == "email_list":
        items = [part.strip() for part in raw.split(",") if part.strip()]
        if not all(_valid_email(i) for i in items):
            raise ValueError("Enter valid email addresses separated by commas")
        return items
    if kind == "tz":
        try:
            ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("Unknown timezone, for example Europe/London") from None
        return raw
    if kind == "path":
        if not raw or not os.path.isabs(raw):
            raise ValueError("Enter a full path starting with /")
        return raw
    if kind == "cron":
        from app.scheduler import digest_trigger  # scheduler imports app.main

        try:
            digest_trigger(raw, ZoneInfo("UTC"))
        except Exception:  # noqa: BLE001 – apscheduler raises assorted errors
            raise ValueError("Enter a five-part cron schedule, for example 0 8 * * MON") from None
        return raw
    return raw


def _show(field: Field, value) -> str:
    if field.kind == "bool":
        return "on" if value else "off"
    if value is None or value == "" or value == []:
        return "–"
    if isinstance(value, list):
        return ", ".join(value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _form_string(field: Field, value) -> str:
    if field.kind == "bool":
        return "on" if value else ""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _logins_from_plain(plain: str | None) -> list[dict]:
    try:
        data = json.loads(plain) if plain is not None else []
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def load_registry_logins(store: Store, box: SecretBox) -> list[dict]:
    """Decrypted registry logins, or [] if none are saved or they can't be read.
    Lets the credential helper read them without building a SettingsService."""
    return logins_from_stored(store.load_settings().get(REGISTRY_LOGINS), box)


def logins_from_stored(stored, box: SecretBox) -> list[dict]:
    """Decrypt the stored `registry_logins` value (None if never saved)."""
    token = stored.get("enc") if isinstance(stored, dict) else None
    plain = box.decrypt(token) if isinstance(token, str) else None
    return _logins_from_plain(plain)


class SettingsService:
    def __init__(self, base: Settings, store: Store, box: SecretBox) -> None:
        self._base = base
        self._store = store
        self._box = box
        self.unreadable: set[str] = set()
        self._warned: set[str] = set()

    def _decrypt(self, name: str, stored) -> str | None:
        token = stored.get("enc") if isinstance(stored, dict) else None
        plain = self._box.decrypt(token) if isinstance(token, str) else None
        if plain is None:
            self.unreadable.add(name)
            if name not in self._warned:
                self._warned.add(name)
                log.warning("Saved value for %s can't be read; treating it as unset", name)
        else:
            self.unreadable.discard(name)
        return plain

    def effective(self) -> Settings:
        base = self._base
        overlay: dict[str, object] = {}
        for name, stored in self._store.load_settings().items():
            field = _BY_NAME.get(name)
            if (field is None or name in base.model_fields_set
                    or name not in type(base).model_fields):
                continue
            if field.kind == "secret":
                plain = self._decrypt(name, stored)
                if plain is not None:
                    overlay[name] = plain
            else:
                overlay[name] = stored
        return type(base).model_validate({**base.model_dump(), **overlay})

    def locked(self, name: str) -> str | None:
        return name.upper() if name in self._base.model_fields_set else None

    def has_secret(self, name: str) -> bool:
        return name in self._store.load_settings() and name not in self.unreadable \
            and self._decrypt(name, self._store.load_settings()[name]) is not None

    def form_values(self, section: str) -> dict[str, str]:
        current = self.effective()
        return {
            f.name: "" if f.kind == "secret" else _form_string(f, getattr(current, f.name))
            for f in FIELDS if f.section == section
        }

    def save(self, section: str, form: Mapping[str, str], user: str) -> list[str]:
        if section not in SECTIONS:
            raise ValueError(f"Unknown settings section: {section}")
        current = self.effective()
        errors: dict[str, str] = {}
        updates: dict[str, object] = {}
        parts: list[str] = []
        changed: list[str] = []
        for field in (f for f in FIELDS if f.section == section):
            if self.locked(field.name):
                continue
            raw = form.get(field.name, "")
            if field.kind == "secret":
                if not raw.strip():
                    continue
                updates[field.name] = {"enc": self._box.encrypt(raw.strip())}
                parts.append(f"{field.label} replaced")
                changed.append(field.name)
                continue
            try:
                value = _parse(field, raw)
            except ValueError as exc:
                errors[field.name] = str(exc)
                continue
            old = getattr(current, field.name)
            if value != old:
                updates[field.name] = value
                parts.append(f"{field.label} {_show(field, old)} → {_show(field, value)}")
                changed.append(field.name)
        if errors:
            raise SettingsError(errors)
        if updates:
            self._store.save_settings(updates)
            self.unreadable.difference_update(n for n in updates if n in self.unreadable)
            self._store.add_audit(user, "settings", f"{SECTIONS[section]}: {', '.join(parts)}")
        return changed

    def save_registry_logins(self, logins: list[dict], user: str, summary: str) -> None:
        token = self._box.encrypt(json.dumps(logins))
        self._store.save_settings({REGISTRY_LOGINS: {"enc": token}})
        self._store.add_audit(user, "settings", summary)

    def registry_logins(self) -> list[dict]:
        stored = self._store.load_settings().get(REGISTRY_LOGINS)
        if stored is None:
            return []
        return _logins_from_plain(self._decrypt(REGISTRY_LOGINS, stored))
