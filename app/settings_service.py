"""Settings kept in the database, layered under environment variables."""
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Literal, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.rules import is_plain_http_url
from app.keys import KeyUnusable, SecretBox
from app.store import Store

log = logging.getLogger(__name__)

REGISTRY_LOGINS = "registry_logins"
GROUP_MAPPINGS = "group_mappings"
# Set on the page only: an environment value must not preset (or lock) these.
UI_ONLY = frozenset({"local_sign_in", "hide_local_for_non_admins", "gravatar",
                     "require_totp_admins", "default_role", "check_on_startup",
                     "random_delay_minutes", "heartbeat_url", "layout_check",
                     "named_volumes_allowed", "layout_allowlist"})
MAX_ALLOWLIST = 50
MAX_HEARTBEAT_URL = 2000
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
                  "cron", "tz", "path", "path_list", "secret", "choice"]
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
    Field("gravatar", "Show Gravatar pictures", "signin", "bool",
          "For anyone without a Microsoft photo, the dashboard looks up a picture on "
          "Gravatar once a day, using a hash of their email address."),
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
    Field("check_on_startup", "Check when the dashboard starts", "checking", "bool",
          "Untick to wait one full interval after a restart before the first check."),
    Field("random_delay_minutes", "Random delay before scheduled checks (minutes)", "checking",
          "int", "Spreads checks out so they don't all hit registries at once. 0 means no delay.",
          0, 60),
    Field("heartbeat_url", "Heartbeat address", "checking", "text",
          "Optional. After each scheduled check this address is fetched, with /fail added "
          "if the check failed. Use https:// or http://, with no username or password.",
          optional=True),
    Field("layout_check", "Check container layout", "checking", "bool",
          "Flags containers whose files live in places that are awkward to back up or move."),
    Field("named_volumes_allowed", "Allow named volumes", "checking", "bool",
          "Don't flag containers that use named Docker volumes."),
    Field("layout_allowlist", "Allowed folders", "checking", "path_list",
          f"Full paths, one per line (up to {MAX_ALLOWLIST}). Mounts under these are never flagged."),
    Field("digest_schedule", "Schedule", "digest", "cron",
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


# --- schedule picker ------------------------------------------------------------------
# A cron field is shown as days and a time when its schedule is that simple
# ("M H * * days"); anything else stays a custom cron line. The form sends
# <name>_mode ("days" or "custom"), <name>_days (comma-separated) and <name>_time.

WEEKDAYS = (("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"), ("thu", "Thu"),
            ("fri", "Fri"), ("sat", "Sat"), ("sun", "Sun"))
_DAY_PLURALS = {"mon": "Mondays", "tue": "Tuesdays", "wed": "Wednesdays", "thu": "Thursdays",
                "fri": "Fridays", "sat": "Saturdays", "sun": "Sundays"}
_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def schedule_picker(schedule: str) -> dict | None:
    """{"days": [...], "time": "HH:MM"} for a days-and-time schedule, else None."""
    from app.scheduler import _cron_weekdays  # scheduler imports app.main

    parts = (schedule or "").split()
    if len(parts) != 5 or parts[2:4] != ["*", "*"]:
        return None
    minute, hour, dow = parts[0], parts[1], parts[4]
    if not (minute.isdigit() and hour.isdigit() and int(minute) < 60 and int(hour) < 24):
        return None
    days = {d for d, _ in WEEKDAYS} if dow == "*" else set(_cron_weekdays(dow).lower().split(","))
    if not days or not days <= {d for d, _ in WEEKDAYS}:
        return None
    return {"days": [d for d, _ in WEEKDAYS if d in days], "time": f"{int(hour):02d}:{int(minute):02d}"}


def schedule_from_picker(days: str, time: str) -> str:
    """The cron line for the picked days and time; ValueError with a message if incomplete."""
    chosen = [d for d, _ in WEEKDAYS if d in {x.strip().lower() for x in (days or "").split(",")}]
    if not chosen:
        raise ValueError("Pick at least one day")
    match = _TIME.fullmatch((time or "").strip())
    if not match:
        raise ValueError("Enter a time, for example 08:00")
    dow = "*" if len(chosen) == 7 else ",".join(d.upper() for d in chosen)
    return f"{int(match[2])} {int(match[1])} * * {dow}"


def schedule_in_words(schedule: str) -> str | None:
    """"on Mondays and Thursdays at 08:00" for a days-and-time schedule, else None."""
    picked = schedule_picker(schedule)
    if picked is None:
        return None
    days = picked["days"]
    if len(days) == 7:
        when = "every day"
    elif days == ["mon", "tue", "wed", "thu", "fri"]:
        when = "on weekdays"
    elif days == ["sat", "sun"]:
        when = "at weekends"
    else:
        names = [_DAY_PLURALS[d] for d in days]
        when = "on " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])
    return f"{when} at {picked['time']}"


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
        if field.name == "heartbeat_url":
            if len(raw) > MAX_HEARTBEAT_URL or not is_plain_http_url(raw):
                raise ValueError("Start with https:// or http:// and don't put a username "
                                 "or password in the address")
            return raw
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
    if kind == "path_list":
        items = [line.strip() for line in raw.splitlines() if line.strip()]
        if len(items) > MAX_ALLOWLIST:
            raise ValueError(f"Enter at most {MAX_ALLOWLIST} paths")
        if not all(os.path.isabs(i) and "\0" not in i for i in items):
            raise ValueError("Enter full paths starting with /, one per line")
        return list(dict.fromkeys(items))
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
        return "\n".join(value) if field.kind == "path_list" else ", ".join(value)
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
            if field.kind == "cron" and form.get(f"{field.name}_mode") == "days":
                try:
                    raw = schedule_from_picker(form.get(f"{field.name}_days", ""),
                                               form.get(f"{field.name}_time", ""))
                except ValueError as exc:
                    errors[field.name] = str(exc)
                    continue
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
                if field.name == "heartbeat_url":  # may carry a token: never audit the address
                    what = "cleared" if value is None else "set" if old is None else "changed"
                    parts.append(f"{field.label} {what}")
                else:
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
