"""Settings kept in the database, layered under environment variables."""
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Literal, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.keys import KeyUnusable, SecretBox
from app.store import Store

log = logging.getLogger(__name__)

REGISTRY_LOGINS = "registry_logins"
GROUP_MAPPINGS = "group_mappings"
# Set on the page only: an environment value must not preset (or lock) these.
UI_ONLY = frozenset({"local_sign_in", "hide_local_for_non_admins",
                     "require_totp_admins", "default_role"})
LOCAL_OFF_MESSAGE = "Turn on Microsoft sign-in first, or you won't be able to sign in"
_GUID = re.compile(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
MAPPABLE_ROLES = {"viewer": "Viewer", "operator": "Operator", "admin": "Admin"}
KEY_UNUSABLE_MESSAGE = "The key file for saved secrets can't be used, so nothing can be saved here. See the logs."


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    section: str
    kind: Literal["text", "int", "float", "bool", "email", "email_list",
                  "cron", "tz", "path", "secret", "choice"]
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    optional: bool = False  # text only: blank clears the value instead of being an error


SECTIONS = {"general": "General", "signin": "Sign-in", "checking": "Checking",
            "digest": "Weekly digest"}

FIELDS: list[Field] = [
    Field("server_name", "Server name", "general", "text",
          "Shown at the top of the dashboard and in email subjects."),
    Field("tz", "Timezone", "general", "tz",
          "Used for the weekly digest schedule, for example Europe/London."),
    Field("public_url", "Dashboard address", "general", "text",
          "Where people reach this dashboard, for example https://dash.example.com. "
          "Used for Microsoft sign-in and links in emails.", optional=True),
    Field("local_sign_in", "Allow local accounts", "signin", "bool",
          "People can sign in with an email address and password kept on this server."),
    Field("hide_local_for_non_admins",
          "Show local sign-in only behind an \"Admin sign-in\" link", "signin", "bool",
          "Keeps the sign-in page to Microsoft for everyone else."),
    Field("require_totp_admins", "Require two-step sign-in for local Admin accounts",
          "signin", "bool", "Local Admins must set up an authenticator app."),
    Field("default_role", "Role for new Microsoft sign-ins", "signin", "choice",
          "What someone can do the first time they sign in with Microsoft.",
          choices=(("pending", "Waiting for approval"), ("viewer", "Viewer"),
                   ("operator", "Operator"))),
    Field("azure_tenant_id", "Directory (tenant) ID", "signin", "text",
          "From your Microsoft Entra app registration.", optional=True),
    Field("azure_client_id", "Application (client) ID", "signin", "text",
          "From your Microsoft Entra app registration.", optional=True),
    Field("azure_client_secret", "Client secret", "signin", "secret",
          "Leave blank to keep the saved one."),
    Field("check_interval_hours", "Check every (hours)", "checking", "float",
          "How often the dashboard looks for newer images.", 0.25, 168),
    Field("health_timeout_seconds", "Health check timeout (seconds)", "checking", "float",
          "How long to wait for a container to become healthy after an update.", 30, 3600),
    Field("mailcow_dir", "Mailcow folder", "checking", "path",
          "Full path to the Mailcow folder on this server."),
    Field("digest_schedule", "Weekly digest schedule", "digest", "cron",
          "Five-part cron schedule, for example 0 8 * * MON for Mondays at 8am."),
    Field("digest_when_none", "Send the digest when everything is up to date", "digest", "bool",
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


def _valid_public_url(value: str) -> bool:
    parts = urlsplit(value)
    if "?" in value or "#" in value or parts.username or parts.password or "@" in parts.netloc:
        return False
    if parts.path not in ("", "/") or parts.query or parts.fragment or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def choices_for(field: Field, current) -> tuple[tuple[str, str], ...]:
    """The choices on offer; the upgrade-only `admin_legacy` shows only while it is set."""
    if field.name == "default_role" and current == "admin_legacy":
        return (*field.choices, ("admin_legacy", "Admin (as in 0.1)"))
    return field.choices


def _parse(field: Field, raw: str, current=None):
    """Turn a form string into a stored value; raise ValueError with a user-facing message."""
    raw = (raw or "").strip()
    kind = field.kind
    if kind == "bool":
        return raw == "on"
    if kind == "choice":
        if raw not in {value for value, _ in choices_for(field, current)}:
            raise ValueError("Choose one of the options")
        return raw
    if kind == "text":
        if not raw:
            if field.optional:
                return None
            raise ValueError("Enter a value")
        if field.name == "public_url":
            if not _valid_public_url(raw):
                raise ValueError("Start with https:// and give only the address, "
                                 "for example https://dash.example.com")
            return raw.rstrip("/")
        if field.name == "azure_tenant_id" and not _GUID.fullmatch(raw):
            raise ValueError("Use the Directory (tenant) ID – the long code, not your domain name")
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


def env_set(base: Settings, name: str) -> bool:
    """True if the environment really sets this field. An empty value (0.1 compose files
    passed `ACS_CONNECTION_STRING: "${ACS_CONNECTION_STRING}"` even when unset) does not
    count; `false` and 0 do."""
    return name not in UI_ONLY and name in base.model_fields_set and getattr(base, name, None) not in (None, "", [])


class SettingsService:
    def __init__(self, base: Settings, store: Store, box: SecretBox) -> None:
        self._base = base
        self._store = store
        self._box = box
        self.unreadable: set[str] = set()
        # Until channels replace the 0.3 digest: email values from a saved channel.
        self.email_fallback: Callable[[], dict] | None = None
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
            if (field is None or env_set(base, name)
                    or name not in type(base).model_fields):
                continue
            if field.kind == "secret":
                plain = self._decrypt(name, stored)
                if plain is not None:
                    overlay[name] = plain
            else:
                overlay[name] = stored
        if self.email_fallback is not None:
            overlay.update({n: v for n, v in self.email_fallback().items()
                            if not env_set(base, n)})
        defaults = {n: type(base).model_fields[n].default for n in UI_ONLY}
        settings = type(base).model_validate({**base.model_dump(), **defaults, **overlay})
        if not settings.local_sign_in and not settings.entra_enabled:
            if "local_sign_in" not in self._warned:
                self._warned.add("local_sign_in")
                log.warning("Local sign-in is turned off but Microsoft sign-in isn't usable; "
                            "keeping local sign-in on")
            settings = settings.model_copy(update={"local_sign_in": True})
        return settings

    def locked(self, name: str) -> str | None:
        return name.upper() if env_set(self._base, name) else None

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
                try:
                    updates[field.name] = {"enc": self._box.encrypt(raw.strip())}
                except KeyUnusable:
                    errors[field.name] = KEY_UNUSABLE_MESSAGE
                    continue
                parts.append(f"{field.label} replaced")
                changed.append(field.name)
                continue
            try:
                value = _parse(field, raw, getattr(current, field.name))
            except ValueError as exc:
                errors[field.name] = str(exc)
                continue
            old = getattr(current, field.name)
            if value != old:
                updates[field.name] = value
                parts.append(f"{field.label} {_show(field, old)} → {_show(field, value)}")
                changed.append(field.name)
        if (section == "signin" and not self.locked("local_sign_in") and "local_sign_in" not in errors
                and updates.get("local_sign_in", current.local_sign_in) is False
                and not self._entra_after(current, updates)):
            errors["local_sign_in"] = LOCAL_OFF_MESSAGE
        if errors:
            raise SettingsError(errors)
        if updates:
            self._store.save_settings(updates)
            self.unreadable.difference_update(n for n in updates if n in self.unreadable)
            self._store.add_audit(user, "settings", f"{SECTIONS[section]}: {', '.join(parts)}")
        return changed

    @staticmethod
    def _entra_after(current: Settings, updates: dict) -> bool:
        def has(name):
            return bool(updates[name]) if name in updates else bool(getattr(current, name))
        return all(has(n) for n in ("public_url", "azure_tenant_id", "azure_client_id",
                                    "azure_client_secret"))

    def group_mappings(self) -> list[dict]:
        stored = self._store.load_settings().get(GROUP_MAPPINGS)
        if not isinstance(stored, list):
            return []
        return [m for m in stored if isinstance(m, dict) and isinstance(m.get("group_id"), str)
                and m.get("role") in MAPPABLE_ROLES]

    def save_group_mappings(self, mappings: list[dict], user: str, summary: str) -> None:
        clean = []
        seen: set[str] = set()
        for m in mappings:
            group_id = str(m.get("group_id", "")).strip()
            role = m.get("role")
            if not _GUID.fullmatch(group_id):
                raise ValueError("Enter the group's Object ID, which looks like a GUID")
            if role not in MAPPABLE_ROLES:
                raise ValueError("Choose Viewer, Operator or Admin")
            if group_id.lower() in seen:
                raise ValueError("That group already has a role")
            seen.add(group_id.lower())
            clean.append({"group_id": group_id.lower(), "role": role})
        self._store.save_settings({GROUP_MAPPINGS: clean})
        self._store.add_audit(user, "settings", summary)

    def save_registry_logins(self, logins: list[dict], user: str, summary: str) -> None:
        token = self._box.encrypt(json.dumps(logins))
        self._store.save_settings({REGISTRY_LOGINS: {"enc": token}})
        self._store.add_audit(user, "settings", summary)

    def registry_logins(self) -> list[dict]:
        stored = self._store.load_settings().get(REGISTRY_LOGINS)
        if stored is None:
            return []
        return _logins_from_plain(self._decrypt(REGISTRY_LOGINS, stored))
