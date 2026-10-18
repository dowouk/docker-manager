"""Periodic update check and weekly digest email."""
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from app.config import Settings
from app.digest import send_digest
from app.main import run_check

log = logging.getLogger(__name__)

# Standard cron weekday numbers: 0 and 7 = Sunday, 1 = Monday … 6 = Saturday.
_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _day_number(value: str) -> int:
    """0–7 as given (7 stays 7 so ranges like 5-7 work), or a day name's number."""
    value = value.strip().lower()
    return int(value) if value.isdigit() else _DAY_NAMES.index(value[:3])


def _cron_weekdays(field: str) -> str:
    """Rewrite a cron day-of-week field that uses numbers as day names.

    APScheduler's from_crontab numbers weekdays from Monday = 0, so "1" would
    mean Tuesday. Names mean the same to both, so numbers are expanded to names.
    """
    if not any(ch.isdigit() for ch in field):
        return field
    try:
        days: set[int] = set()
        for part in field.split(","):
            span, _, step = part.partition("/")
            if span == "*":
                lo, hi = 0, 6
            elif "-" in span:
                a, b = span.split("-", 1)
                lo, hi = _day_number(a), _day_number(b)
            else:
                lo = hi = _day_number(span)
            if not (0 <= lo <= hi <= 7):
                raise ValueError(part)
            days.update(n % 7 for n in range(lo, hi + 1, int(step) if step else 1))
    except ValueError:
        return field  # leave it for APScheduler to reject with its own message
    return ",".join(_DAY_NAMES[n] for n in sorted(days))


def digest_trigger(schedule: str, tz) -> CronTrigger:
    fields = schedule.split()
    if len(fields) == 5:
        fields[4] = _cron_weekdays(fields[4])
    return CronTrigger.from_crontab(" ".join(fields), timezone=tz)


async def scheduled_check(app: FastAPI) -> None:
    try:
        await run_check(app)
    except Exception:  # noqa: BLE001 – keep the scheduler alive
        log.exception("Scheduled check failed")


async def scheduled_digest(app: FastAPI) -> None:
    try:
        result = await run_check(app)
    except Exception:  # noqa: BLE001 – keep the scheduler alive
        log.exception("Scheduled digest: the check failed")
        return
    try:
        outcome = await asyncio.to_thread(
            send_digest, app.state.settings, result, app.state.email_client_factory
        )
        log.info("Scheduled digest: %s", outcome)
    except Exception as exc:  # noqa: BLE001 – keep the scheduler alive
        # Type only: the message can contain the email connection string.
        log.error("Scheduled digest failed (%s)", type(exc).__name__)


def purge_sessions(app: FastAPI) -> None:
    try:
        app.state.sessions.purge_expired()
    except Exception:  # noqa: BLE001 – keep the scheduler alive
        log.exception("Session purge failed")
    try:
        app.state.users.purge_failures(timedelta(days=1))
    except Exception:  # noqa: BLE001
        log.exception("Sign-in failure purge failed")


def _add_digest_job(scheduler: AsyncIOScheduler, app: FastAPI, settings: Settings, tz) -> None:
    scheduler.add_job(
        scheduled_digest,
        digest_trigger(settings.digest_schedule, tz),
        args=[app],
        id="digest",
        max_instances=1,
        coalesce=True,
    )


def build_scheduler(app: FastAPI, settings: Settings) -> AsyncIOScheduler:
    tz = ZoneInfo(settings.tz)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        scheduled_check,
        IntervalTrigger(hours=settings.check_interval_hours, timezone=tz),
        args=[app],
        id="check",
        next_run_time=datetime.now(tz),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        purge_sessions,
        IntervalTrigger(hours=1, timezone=tz),
        args=[app],
        id="sessions-purge",
        max_instances=1,
        coalesce=True,
    )
    if settings.digest_enabled:
        _add_digest_job(scheduler, app, settings, tz)
    return scheduler


def reschedule(app: FastAPI) -> None:
    """Apply changed settings to the running scheduler. No check runs immediately."""
    scheduler = app.state.scheduler
    settings = app.state.settings
    tz = ZoneInfo(settings.tz)
    trigger = IntervalTrigger(hours=settings.check_interval_hours, timezone=tz)
    current = scheduler.get_job("check").trigger
    # Rescheduling restarts the interval, so only do it when the interval or zone changed.
    if current.interval != trigger.interval or str(current.timezone) != str(tz):
        scheduler.reschedule_job("check", trigger=trigger)
    if scheduler.get_job("digest"):
        scheduler.remove_job("digest")
    if settings.digest_enabled:
        _add_digest_job(scheduler, app, settings, tz)
