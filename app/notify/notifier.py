"""The notifier: turns events into messages and sends them to the channels that want them.

Nothing here may delay or break what triggered it. `fire` schedules a background task; every
failure is caught, recorded per channel (a fixed reason, never provider text) and logged as the
channel name and exception type only.
"""
import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx

from app.checker import CheckResult, ContainerStatus, Status
from app.config import Settings
from app.digest import layout_lines, layout_summary, notifiable_layout
from app.imageref import parse_image_ref
from app.inventory import ContainerInfo
from app.links import release_notes_url
from app.notify import messages
from app.notify.channels import Channel, ChannelStore
from app.notify.messages import Rendered, UpdateItem
from app.notify.providers import (
    LOGIN_REFUSED, NOT_PRIVATE, NO_ANSWER, PROVIDERS, REJECTED, UNREACHABLE, Message, NotifyError,
)
from app.skips import SkipStore, visible_result
from app.store import Store

log = logging.getLogger(__name__)

SEND_TIMEOUT = 30
UNKNOWN_PROVIDER = "This channel's provider isn't available"
NO_SUCH_CHANNEL = "That channel no longer exists"
FIXED_REASONS = frozenset({LOGIN_REFUSED, UNREACHABLE, REJECTED, NO_ANSWER, NOT_PRIVATE})
FAILURES_AFTER = 2  # consecutive failed checks before the first alert
# "Updates found" waits this long, so an update applied straight away (and the checks the
# update jobs run) never sends a message.
UPDATES_FOUND_DELAY = timedelta(hours=1)
_BUILD = (Status.NEWER_BUILD, Status.PULLED_NOT_RECREATED)

DIGEST_SENT = "sent"
DIGEST_NOTHING = "skipped – nothing to report"
DIGEST_NO_CHANNEL = "skipped – no channel gets the digest"
DIGEST_FAILED = "failed"


def check_failed(result: CheckResult | None) -> bool:
    """Every checkable container ended in an error (one image's error is not a failed check)."""
    if result is None:
        return True
    checkable = [cs for cs in result.containers
                 if cs.status not in (Status.NOT_CHECKABLE, Status.NOT_WATCHED)]
    return bool(checkable) and all(cs.status == Status.ERROR for cs in checkable)


def _tag(cs: ContainerStatus) -> str:
    return parse_image_ref(cs.container.image_ref).tag or "latest"


def _update_items(result: CheckResult, notes_for: Callable[[ContainerInfo], str | None] | None = None
                  ) -> dict[str, tuple[str, UpdateItem]]:
    """key -> (signature, item) for every actionable update in the result."""
    items: dict[str, tuple[str, UpdateItem]] = {}
    notes = None

    def add(key: str, signature: str, name: str, image: str, change: str) -> None:
        items[key] = (signature, UpdateItem(key, name, image, change, notes))

    mc = result.mailcow
    if mc and not mc.error and mc.update_available:
        latest = mc.latest_release or ""
        add("mailcow", latest, "Mailcow", "",
            f"{mc.current_version or 'unknown'} → {latest or 'unknown'}")
    for cs in result.containers:
        if not cs.notify:
            continue  # the container's `notify` rule keeps it out of "Updates found"
        name, image = cs.container.name, cs.container.image_ref
        notes = notes_for(cs.container) if notes_for else None
        if cs.status in _BUILD:
            add(f"{name}:build", cs.remote_digest or "", name, image, "Newer build available")
        if cs.minor_update:
            add(f"{name}:minor", cs.minor_update, name, image, f"{_tag(cs)} → {cs.minor_update}")
        if cs.major_update:
            add(f"{name}:major", cs.major_update, name, image,
                f"{_tag(cs)} → {cs.major_update} (update by hand)")
    return items


def current_updates(result: CheckResult) -> dict[str, str]:
    """key -> signature for every actionable item; a changed signature is a new update."""
    return {key: sig for key, (sig, _) in _update_items(result).items()}


class Notifier:
    def __init__(self, store: Store, channels: ChannelStore,
                 settings_getter: Callable[[], Settings], http: httpx.AsyncClient,
                 providers=PROVIDERS,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 background: set | None = None, kind_labels: dict[str, str] | None = None,
                 skips_getter: Callable[[], SkipStore | None] | None = None,
                 rules_snapshot: Callable[[], Callable[[ContainerInfo], object]] | None = None,
                 updates_delay: timedelta = UPDATES_FOUND_DELAY):
        self._store = store
        self._channels = channels
        self._settings = settings_getter
        self._http = http
        self._providers = providers
        self._clock = clock
        self._background = background if background is not None else set()
        self._kind_labels = kind_labels or {}
        self._skips = skips_getter
        self._rules_snapshot = rules_snapshot
        self._updates_delay = updates_delay

    def _visible(self, result: CheckResult) -> tuple[CheckResult, int]:
        """The result with skipped and snoozed updates cleared, and how many that hid."""
        skips = self._skips() if self._skips else None
        if skips is None:
            return result, 0
        shown, hidden = visible_result(result, skips)
        # A container left out of the digest by its `notify` rule doesn't count as skipped.
        return shown, sum(1 for cs, _skip in hidden if cs is None or cs.notify)

    def _notes_for(self) -> Callable[[ContainerInfo], str | None] | None:
        if self._rules_snapshot is None:
            return None
        rules_for = self._rules_snapshot()
        return lambda info: release_notes_url(info, rules_for(info))

    # -- sending ---------------------------------------------------------------------------
    async def _deliver(self, channel: Channel, event: str, rendered: Rendered) -> str | None:
        """Send to one channel and record the outcome. Returns the fixed reason, or None."""
        reason: str | None = None
        provider = self._providers.get(channel.provider)
        if provider is None:
            reason = UNKNOWN_PROVIDER
            log.warning("Notification to %s skipped (unknown provider)", channel.name)
        else:
            message = Message(rendered[0], rendered[1], rendered[2], tuple(channel.recipients))
            try:
                await asyncio.wait_for(
                    provider.send(channel.config, message, self._http), SEND_TIMEOUT)
            except NotifyError as exc:
                reason = str(exc) if str(exc) in FIXED_REASONS else REJECTED
                log.warning("Notification to %s failed (NotifyError)", channel.name)
            except asyncio.TimeoutError:
                reason = NO_ANSWER
                log.warning("Notification to %s failed (TimeoutError)", channel.name)
            except Exception as exc:  # noqa: BLE001 – never raise; the text may hold secrets
                reason = REJECTED
                log.warning("Notification to %s failed (%s)", channel.name, type(exc).__name__)
        self._record(channel, event, reason)
        return reason

    def _record(self, channel: Channel, event: str, reason: str | None) -> None:
        try:
            self._store.set_meta(f"notify_last:{channel.id}", json.dumps({
                "at": self._clock().isoformat(), "ok": reason is None,
                "reason": reason, "event": event}))
        except Exception as exc:  # noqa: BLE001
            log.warning("Recording the notification result failed (%s)", type(exc).__name__)

    async def send(self, event: str, rendered: Rendered | tuple[str, str, str] | None) -> int:
        """Send to every channel subscribed to the event; returns how many succeeded."""
        if rendered is None:
            return 0
        try:
            channels = self._channels.subscribed(event)
            reasons = await asyncio.gather(
                *(self._deliver(c, event, Rendered(*rendered)) for c in channels))
        except Exception as exc:  # noqa: BLE001
            log.warning("Notification for %s failed (%s)", event, type(exc).__name__)
            return 0
        return sum(1 for r in reasons if r is None)

    def fire(self, event: str, rendered: Rendered | tuple[str, str, str] | None) -> None:
        """Send in the background: the caller never waits."""
        if rendered is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.send(event, rendered))
        except RuntimeError:
            log.warning("Notification for %s not sent (no event loop)", event)
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _fire_message(self, event: str, build: Callable[[Settings], Rendered]) -> None:
        try:
            self.fire(event, build(self._settings()))
        except Exception as exc:  # noqa: BLE001
            log.warning("Notification for %s not sent (%s)", event, type(exc).__name__)

    def last_result(self, channel_id: str) -> dict | None:
        try:
            raw = self._store.get_meta(f"notify_last:{channel_id}")
            data = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    async def test(self, channel_id: str) -> str | None:
        """Send a test message to this channel only; None on success, else the fixed reason."""
        channel = self._channels.get(channel_id)
        if channel is None:
            return NO_SUCH_CHANNEL
        try:
            rendered = messages.test_message(channel.name, self._settings())
        except Exception as exc:  # noqa: BLE001
            log.warning("Notification to %s failed (%s)", channel.name, type(exc).__name__)
            return REJECTED
        return await self._deliver(channel, "test", rendered)

    # -- events ----------------------------------------------------------------------------
    async def digest(self, result: CheckResult) -> str:
        if not self._channels.subscribed("digest"):
            return DIGEST_NO_CHANNEL
        try:
            shown, skipped = self._visible(result)
            settings = self._settings()
            if not settings.layout_check:
                shown = dataclasses.replace(shown, layout=None)  # switched off: nothing to report
            rendered = messages.digest_message(shown, settings, skipped, self._notes_for())
        except Exception as exc:  # noqa: BLE001
            log.warning("Digest could not be rendered (%s)", type(exc).__name__)
            return DIGEST_FAILED
        if rendered is None:
            return DIGEST_NOTHING
        return DIGEST_SENT if await self.send("digest", rendered) else DIGEST_FAILED

    def after_check(self, result: CheckResult | None, error: bool) -> None:
        """Called after every check: failure alerts, recovery and newly found updates."""
        try:
            if error or result is None:
                self._check_failed(True)
                return
            failed = check_failed(result)
            self._check_failed(failed)
            if not failed:
                self._updates(result)
            self._layout(result)
        except Exception as exc:  # noqa: BLE001 – never break the check
            log.warning("Post-check notifications failed (%s)", type(exc).__name__)

    def _check_failed(self, failed: bool) -> None:
        count = int(self._store.get_meta("check_failures") or 0)
        if failed:
            count += 1
            self._store.set_meta("check_failures", str(count))
            if count == FAILURES_AFTER:
                self._store.set_meta("check_alerted", "1")
                self._fire_message("check_failing",
                                   lambda s: messages.check_failing_message(count, s))
            return
        if count:
            self._store.set_meta("check_failures", "0")
        if self._store.get_meta("check_alerted") == "1":
            self._store.set_meta("check_alerted", "0")
            # Recovery travels with the same event as the alert ("Check failing or recovered").
            self._fire_message("check_failing", messages.check_recovered_message)

    @staticmethod
    def _errored(result: CheckResult) -> set[str]:
        errored = {cs.container.name for cs in result.containers if cs.status == Status.ERROR}
        if result.mailcow and result.mailcow.error:
            errored.add("mailcow")
        return errored

    def _updates(self, result: CheckResult) -> None:
        shown, _ = self._visible(result)
        items = _update_items(shown, self._notes_for())
        previous = self._meta_dict("notified_updates")
        current = {key: sig for key, (sig, _) in items.items()}
        # An item whose lookup errored this time isn't "fixed": remember it so it doesn't
        # notify again when the lookup works.
        errored = self._errored(result)
        for key, sig in previous.items():
            if key not in current and (key.rpartition(":")[0] or key) in errored:
                current[key] = sig
        new = [key for key, (sig, _item) in items.items() if previous.get(key) != sig]
        self._store.set_meta("notified_updates", json.dumps(current))
        # New updates wait UPDATES_FOUND_DELAY before they are sent (see flush_updates).
        pending = self._meta_dict("pending_updates")
        due = (self._clock() + self._updates_delay).isoformat()
        for key in new:
            pending[key] = {"sig": items[key][0], "due": due}
        self._send_due(items, pending, errored)

    def _meta_dict(self, key: str) -> dict:
        try:
            value = json.loads(self._store.get_meta(key) or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _send_due(self, items: dict, pending: dict, errored: set[str]) -> None:
        """Send the held updates whose wait is over and that are still on offer. One that was
        applied, skipped or replaced by a newer one in the meantime is dropped."""
        now = self._clock()
        keep, send = {}, []
        for key, held in pending.items():
            if not isinstance(held, dict) or "sig" not in held or "due" not in held:
                continue
            if key not in items:
                if (key.rpartition(":")[0] or key) in errored:
                    keep[key] = held  # the lookup failed this time: not fixed, keep waiting
                continue
            sig, item = items[key]
            if sig != held["sig"]:
                continue
            try:
                ready = datetime.fromisoformat(held["due"]) <= now
            except (TypeError, ValueError):
                ready = True
            if ready:
                send.append(item)
            else:
                keep[key] = held
        self._store.set_meta("pending_updates", json.dumps(keep))
        if send and self._channels.subscribed("updates_found"):
            self._fire_message("updates_found",
                               lambda s: messages.updates_found_message(send, s))

    def flush_updates(self) -> None:
        """Send held "Updates found" whose wait is over, against the latest stored check.
        Run every few minutes by the scheduler, so it needn't wait for the next check."""
        try:
            if not self._meta_dict("pending_updates"):
                return
            latest = self._store.latest_check()
            if not latest:
                return
            result = CheckResult.from_dict(latest[1])
            if check_failed(result):
                return
            shown, _ = self._visible(result)
            self._send_due(_update_items(shown, self._notes_for()),
                           self._meta_dict("pending_updates"), self._errored(result))
        except Exception as exc:  # noqa: BLE001 – never break the scheduler
            log.warning("Held update notifications failed (%s)", type(exc).__name__)

    def job_finished(self, kind: str, target: str, status: str, user: str, job_id: int) -> None:
        if status not in ("failed", "succeeded"):
            return
        event = "job_failed" if status == "failed" else "job_succeeded"
        label = self._kind_labels.get(kind, kind)
        self._fire_message(event, lambda s: messages.job_message(
            label, target, status, user, job_id, s))

    def user_pending(self, name: str, upn: str) -> None:
        self._fire_message("user_pending", lambda s: messages.user_pending_message(name, upn, s))

    def _layout(self, result: CheckResult) -> None:
        """Notify about layout problems not seen before. The first run with a list is only a
        baseline; `None` means no layout check ran, so nothing is read or written."""
        if result.layout is None:
            return
        problems = notifiable_layout(result)
        try:
            raw = json.loads(self._store.get_meta("notified_layout") or "null")
        except ValueError:
            raw = None
        previous = set(raw) if isinstance(raw, list) else None
        self._store.set_meta("notified_layout", json.dumps(sorted(p.key for p in problems)))
        if previous is None:
            return
        new = [p for p in problems if p.key not in previous]
        if new and self._channels.subscribed("layout_problems"):
            lines, summary = layout_lines(new), layout_summary(new)
            self._fire_message("layout_problems",
                               lambda s: messages.layout_message(lines, s, summary))
