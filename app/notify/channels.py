"""Notification channels: where messages go, which events they carry, and how they are stored.

All saved channels live in one SecretBox-encrypted settings row (like registry logins), so
provider secrets never sit in the database in clear. The environment can supply one extra,
read-only channel (the 0.2 `ACS_CONNECTION_STRING`, `DIGEST_FROM`, `DIGEST_TO` trio).
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Callable

from app.config import Settings
from app.keys import KeyUnusable, SecretBox
from app.notify.providers import EMAIL_ADDRESS, PROVIDERS
from app.settings_service import KEY_UNUSABLE_MESSAGE, env_set
from app.store import Store, _now

log = logging.getLogger(__name__)

CHANNELS_KEY = "notification_channels"
MIGRATED_META = "email_channel_migrated"
ENV_ID = "env"
LEGACY_FIELDS = ("acs_connection_string", "digest_from", "digest_to")
MAX_RECIPIENTS = 20
UNREADABLE: dict = {}  # sentinel from _legacy_values

EVENTS: dict[str, str] = {
    "digest": "Weekly digest",
    "updates_found": "Updates found",
    "job_failed": "Update failed",
    "job_succeeded": "Update finished",
    "check_failing": "Check failing or recovered",
    "layout_problems": "Layout problems",
    "user_pending": "User waiting for approval",
}
DEFAULT_EVENTS = frozenset({"digest", "job_failed", "check_failing", "layout_problems",
                            "user_pending"})


@dataclass
class Channel:
    id: str
    name: str
    provider: str
    config: dict
    recipients: list[str]
    events: set[str]
    enabled: bool = True
    readonly: bool = False


class ChannelError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _dump(channel: Channel) -> dict:
    return {"id": channel.id, "name": channel.name, "provider": channel.provider,
            "config": channel.config, "recipients": channel.recipients,
            "events": sorted(channel.events), "enabled": channel.enabled}


def _load(raw: object) -> Channel | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    try:
        return Channel(
            id=raw["id"], name=str(raw.get("name", "")), provider=str(raw.get("provider", "")),
            config={str(k): str(v) for k, v in dict(raw.get("config") or {}).items()},
            recipients=[str(r) for r in raw.get("recipients") or []],
            events={str(e) for e in raw.get("events") or [] if e in EVENTS},
            enabled=bool(raw.get("enabled", True)),
        )
    except (TypeError, ValueError):
        return None


def _dedupe(recipients: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for r in (x.strip() for x in recipients):
        if r and r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


class ChannelStore:
    def __init__(self, store: Store, box: SecretBox, settings_getter: Callable[[], Settings]):
        self._store = store
        self._box = box
        self._settings = settings_getter
        self.unreadable = False

    # -- reading ---------------------------------------------------------------------------
    def _parse(self, stored) -> list[Channel] | None:
        """Channels from the stored value; None if it can't be read."""
        if stored is None:
            return []
        token = stored.get("enc") if isinstance(stored, dict) else None
        plain = self._box.decrypt(token) if isinstance(token, str) else None
        try:
            data = json.loads(plain) if plain is not None else None
        except ValueError:
            data = None
        if not isinstance(data, list):
            return None
        return [c for c in (_load(d) for d in data) if c is not None]

    def _saved(self) -> list[Channel]:
        channels = self._parse(self._store.load_settings().get(CHANNELS_KEY))
        self._note_readable(channels is not None)
        return channels or []

    def _note_readable(self, readable: bool) -> None:
        if not readable and not self.unreadable:
            log.warning("Saved notification channels can't be read; treating them as none")
        self.unreadable = not readable

    def _locked(self, db) -> list[Channel]:
        """Read inside a write transaction; refuse if the stored row can't be read."""
        row = db.execute("SELECT value FROM settings WHERE name = ?", (CHANNELS_KEY,)).fetchone()
        channels = self._parse(json.loads(row[0]) if row else None)
        self._note_readable(channels is not None)
        if channels is None:
            raise ChannelError({"channel": KEY_UNUSABLE_MESSAGE})
        return channels

    def _put(self, db, channels: list[Channel]) -> None:
        token = self._box.encrypt(json.dumps([_dump(c) for c in channels]))
        db.execute("INSERT INTO settings (name, value) VALUES (?, ?) "
                   "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                   (CHANNELS_KEY, json.dumps({"enc": token})))

    @staticmethod
    def _audit(db, user: str, summary: str) -> None:
        db.execute("INSERT INTO audit (at, user, kind, summary) VALUES (?, ?, ?, ?)",
                   (_now(), user, "settings", summary))

    def _env_channel(self) -> Channel | None:
        base = self._settings()
        if not all(env_set(base, n) for n in LEGACY_FIELDS):
            return None
        return Channel(ENV_ID, "Email (from environment)", "acs",
                       {"connection_string": base.acs_connection_string or "",
                        "sender": base.digest_from or ""},
                       list(base.digest_to), {"digest"}, True, True)

    def list(self) -> list[Channel]:
        env = self._env_channel()
        saved = self._saved()
        return [env, *saved] if env else saved

    def get(self, channel_id: str) -> Channel | None:
        return next((c for c in self.list() if c.id == channel_id), None)

    def subscribed(self, event: str) -> list[Channel]:
        return [c for c in self.list() if c.enabled and event in c.events]

    def has_secret(self, channel_id: str, field_name: str) -> bool:
        channel = self.get(channel_id)
        provider = PROVIDERS.get(channel.provider) if channel else None
        if channel is None or provider is None:
            return False
        is_secret = any(f.name == field_name and f.kind == "secret" for f in provider.fields)
        return is_secret and bool(channel.config.get(field_name))

    def legacy_email(self) -> dict:
        """The 0.3 digest's three values from the first saved ACS channel that carries the
        digest, so the old digest keeps working until channels replace it."""
        for c in self._saved():
            if c.provider == "acs" and c.enabled and "digest" in c.events:
                return {"acs_connection_string": c.config.get("connection_string"),
                        "digest_from": c.config.get("sender"), "digest_to": list(c.recipients)}
        return {}

    # -- writing ---------------------------------------------------------------------------
    def save(self, channel: Channel, form_config: dict, user: str) -> Channel:
        if channel.readonly or channel.id == ENV_ID:
            raise ChannelError({"channel": "This channel comes from the environment and can't be changed here"})
        try:
            with self._store.write_tx() as db:
                saved = self._locked(db)
                result, verb = self._validated(channel, form_config, saved)
                channels = [result if c.id == result.id else c for c in saved]
                if verb == "added":
                    channels.append(result)
                self._put(db, channels)
                self._audit(db, user, f"Notification channel {verb}: {result.name}")
        except KeyUnusable:
            raise ChannelError({"channel": KEY_UNUSABLE_MESSAGE}) from None
        return result

    def _validated(self, channel: Channel, form_config: dict,
                   saved: list[Channel]) -> tuple[Channel, str]:
        existing = next((c for c in saved if c.id == channel.id), None) if channel.id else None
        if channel.id and existing is None:
            raise ChannelError({"channel": "That channel no longer exists"})
        errors: dict[str, str] = {}
        name = (channel.name or "").strip()
        if not 1 <= len(name) <= 60:
            errors["name"] = "Enter a name of 1 to 60 characters"
        provider = PROVIDERS.get(channel.provider)
        if provider is None:
            errors["provider"] = "Choose a provider"
        recipients = _dedupe(channel.recipients)
        if not 1 <= len(recipients) <= MAX_RECIPIENTS:
            errors["recipients"] = f"Enter 1 to {MAX_RECIPIENTS} email addresses"
        elif not all(EMAIL_ADDRESS.match(r) for r in recipients):
            errors["recipients"] = "Enter valid email addresses"
        if not channel.events <= EVENTS.keys():
            errors["events"] = "Choose from the listed events"

        config: dict[str, str] = {}
        if provider is not None:
            keep = existing is not None and existing.provider == channel.provider
            for f in provider.fields:
                value = str(form_config.get(f.name) or "").strip()
                if not value and f.kind == "secret" and keep:
                    value = existing.config.get(f.name, "")
                if value:
                    config[f.name] = value
            errors.update({k: v for k, v in provider.validate(config).items() if k not in errors})
        if errors:
            raise ChannelError(errors)
        result = Channel(existing.id if existing else secrets.token_hex(8), name,
                         channel.provider, config, recipients, set(channel.events),
                         channel.enabled)
        return result, "changed" if existing else "added"

    def delete(self, channel_id: str, user: str) -> None:
        if channel_id == ENV_ID:
            raise ChannelError({"channel": "This channel comes from the environment and can't be removed here"})
        try:
            with self._store.write_tx() as db:
                saved = self._locked(db)
                target = next((c for c in saved if c.id == channel_id), None)
                if target is None:
                    return
                self._put(db, [c for c in saved if c.id != channel_id])
                self._audit(db, user, f"Notification channel removed: {target.name}")
        except KeyUnusable:
            raise ChannelError({"channel": KEY_UNUSABLE_MESSAGE}) from None

    # -- 0.2 migration ---------------------------------------------------------------------
    def migrate_0_2_email(self) -> None:
        """Turn the email settings saved on the 0.2 Settings page into a channel, once.
        One transaction: the channel, the row removal and the marker land together."""
        with self._store.write_tx() as db:
            if db.execute("SELECT 1 FROM meta WHERE key = ?", (MIGRATED_META,)).fetchone():
                return
            rows = {n: json.loads(v) for n, v in db.execute("SELECT name, value FROM settings")}
            present = [n for n in LEGACY_FIELDS if n in rows]
            if present and self._env_channel() is None:
                values = self._legacy_values(rows)
                if values is UNREADABLE:
                    log.warning("The saved email settings can't be read; not migrated yet")
                    return  # keep the rows and try again next start
                if values is not None:
                    try:
                        saved = self._locked(db)
                        self._put(db, [*saved, self._migrated_channel(values)])
                    except (KeyUnusable, ChannelError) as exc:
                        log.warning("Email settings not migrated yet (%s)", type(exc).__name__)
                        return
            for name in present:
                db.execute("DELETE FROM settings WHERE name = ?", (name,))
            if present:
                self._audit(db, "system", "Email settings moved to notification channels")
            db.execute("INSERT INTO meta (key, value) VALUES (?, '1') "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (MIGRATED_META,))

    def _legacy_values(self, rows: dict) -> dict | None:
        """Env values win over saved rows; None if incomplete, UNREADABLE if the key fails."""
        base = self._settings()
        conn = rows.get("acs_connection_string")
        if env_set(base, "acs_connection_string"):
            conn_plain = base.acs_connection_string
        else:
            token = conn.get("enc") if isinstance(conn, dict) else None
            conn_plain = self._box.decrypt(token) if isinstance(token, str) else None
        sender = base.digest_from if env_set(base, "digest_from") else rows.get("digest_from")
        to = list(base.digest_to) if env_set(base, "digest_to") else rows.get("digest_to")
        if conn_plain is None and not env_set(base, "acs_connection_string") \
                and "acs_connection_string" in rows:
            return UNREADABLE
        if not (conn_plain and sender and isinstance(to, list) and to):
            return None
        return {"connection_string": conn_plain, "sender": str(sender),
                "recipients": [str(r) for r in to]}

    @staticmethod
    def _migrated_channel(values: dict) -> Channel:
        # Every recipient comes across: the cap applies to the edit form only.
        return Channel(secrets.token_hex(8), "Email", "acs",
                       {"connection_string": values["connection_string"],
                        "sender": values["sender"]}, _dedupe(values["recipients"]), {"digest"})
