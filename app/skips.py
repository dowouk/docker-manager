"""Skip and snooze updates: hide one update (or any update of a kind) for a while.

Item keys match the notifier's (`"<container name>:build|minor|major"`, `"mailcow"`);
skips are recorded against the container's rule identity (`container_key`).
"""
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.checker import CheckResult, ContainerStatus, Status
from app.inventory import container_key
from app.store import Store, _from_iso, _to_iso

MAX_REASON = 200
KINDS = ("build", "minor", "major", "mailcow")
_BUILD = (Status.NEWER_BUILD, Status.PULLED_NOT_RECREATED)
KIND_LABELS = {"build": "Newer build", "minor": "New version", "major": "Major version",
               "mailcow": "Mailcow release"}


@dataclass(frozen=True)
class ItemRef:
    key: str                  # the notifier's item key
    container_key: str        # what a skip record is stored against
    kind: str
    value: str                # build: remote digest; minor/major: tag; mailcow: release
    container_id: str | None  # None for Mailcow


@dataclass(frozen=True)
class Skip:
    id: int
    key: str                  # container key ("mailcow" for Mailcow)
    kind: str
    value: str | None         # None for a snooze
    until: datetime | None    # None for a skip of one version
    user: str
    reason: str
    created_at: datetime

    @property
    def snoozed(self) -> bool:
        return self.until is not None


def clean_reason(reason: str | None) -> str:
    """One line, at most MAX_REASON characters."""
    return " ".join((reason or "").split())[:MAX_REASON]


def container_items(cs: ContainerStatus) -> list[ItemRef]:
    c = cs.container
    key, name = container_key(c), c.name
    items = []
    if cs.status in _BUILD and cs.remote_digest:
        items.append(ItemRef(f"{name}:build", key, "build", cs.remote_digest, c.id))
    if cs.minor_update:
        items.append(ItemRef(f"{name}:minor", key, "minor", cs.minor_update, c.id))
    if cs.major_update:
        items.append(ItemRef(f"{name}:major", key, "major", cs.major_update, c.id))
    return items


def mailcow_item(result: CheckResult) -> ItemRef | None:
    mc = result.mailcow
    if mc and not mc.error and mc.update_available and mc.latest_release:
        return ItemRef("mailcow", "mailcow", "mailcow", mc.latest_release, None)
    return None


def update_items(result: CheckResult) -> list[ItemRef]:
    """One per actionable update: build -> remote digest, minor/major -> tag, Mailcow -> release."""
    items = [i for cs in result.containers for i in container_items(cs)]
    mc = mailcow_item(result)
    return ([mc] if mc else []) + items


def _matching(item: ItemRef, active: list[Skip], now: datetime) -> Skip | None:
    for s in active:
        if s.key != item.container_key or s.kind != item.kind:
            continue
        if s.until is None:
            if s.value == item.value:
                return s
        elif s.until > now:
            return s
    return None


class SkipStore:
    def __init__(self, store: Store,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._store = store
        self._clock = clock

    @staticmethod
    def _row(r) -> Skip:
        return Skip(r[0], r[1], r[2], r[3], _from_iso(r[4]), r[5], r[6], _from_iso(r[7]))

    def _add(self, key: str, kind: str, value: str | None, until: datetime | None,
             user: str, reason: str) -> Skip:
        if kind not in KINDS:
            raise ValueError("Unknown kind")
        reason = clean_reason(reason)
        now = self._clock()
        with self._store.write_tx() as db:
            # A new skip or snooze replaces the earlier one for the same container and kind.
            db.execute("DELETE FROM skips WHERE key = ? AND kind = ?", (key, kind))
            cur = db.execute(
                "INSERT INTO skips (key, kind, value, until, user, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, kind, value, _to_iso(until) if until else None, user, reason, _to_iso(now)))
            return Skip(cur.lastrowid, key, kind, value, until, user, reason, now)

    def skip(self, container_key: str, kind: str, value: str, user: str, reason: str = "") -> Skip:
        if not value:
            raise ValueError("Nothing to skip")
        return self._add(container_key, kind, value, None, user, reason)

    def snooze(self, container_key: str, kind: str, days: Literal[7, 30],
               user: str, reason: str = "") -> Skip:
        if days not in (7, 30):
            raise ValueError("Snooze for 7 or 30 days")
        return self._add(container_key, kind, None, self._clock() + timedelta(days=days),
                         user, reason)

    def get(self, skip_id: int) -> Skip | None:
        with self._store._lock:
            r = self._store._db.execute(
                "SELECT id, key, kind, value, until, user, reason, created_at "
                "FROM skips WHERE id = ?", (skip_id,)).fetchone()
        return self._row(r) if r else None

    def remove(self, skip_id: int, user: str) -> Skip | None:
        """Delete the skip; returns it (None if it was already gone)."""
        with self._store.write_tx() as db:
            r = db.execute(
                "SELECT id, key, kind, value, until, user, reason, created_at "
                "FROM skips WHERE id = ?", (skip_id,)).fetchone()
            if r is None:
                return None
            db.execute("DELETE FROM skips WHERE id = ?", (skip_id,))
        return self._row(r)

    def active(self) -> list[Skip]:
        """Skips in force; expired snoozes are left out and purged."""
        now = _to_iso(self._clock())
        with self._store.write_tx() as db:
            db.execute("DELETE FROM skips WHERE until IS NOT NULL AND until <= ?", (now,))
            rows = db.execute(
                "SELECT id, key, kind, value, until, user, reason, created_at "
                "FROM skips ORDER BY id DESC").fetchall()
        return [self._row(r) for r in rows]

    def is_skipped(self, item: ItemRef) -> Skip | None:
        return _matching(item, self.active(), self._clock())


def visible_result(result: CheckResult, skips: SkipStore
                   ) -> tuple[CheckResult, list[tuple[ContainerStatus | None, Skip]]]:
    """A copy of the result with skipped updates cleared, and what was hidden.

    A hidden Mailcow update appears as `(None, skip)`.
    """
    active = skips.active()
    now = skips._clock()
    hidden: list[tuple[ContainerStatus | None, Skip]] = []
    containers = []
    for cs in result.containers:
        new = cs
        for item in container_items(cs):
            skip = _matching(item, active, now)
            if skip is None:
                continue
            hidden.append((cs, skip))
            if new is cs:
                new = replace(cs)
            if item.kind == "build":
                new.status = Status.UP_TO_DATE
            elif item.kind == "minor":
                new.minor_update = None
            else:
                new.major_update = None
        containers.append(new)
    mailcow = result.mailcow
    item = mailcow_item(result)
    if item is not None and (skip := _matching(item, active, now)) is not None:
        hidden.append((None, skip))
        mailcow = replace(mailcow, update_available=False)
    return replace(result, containers=containers, mailcow=mailcow), hidden
