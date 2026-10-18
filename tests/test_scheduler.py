from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import httpx
import pytest
import respx

from app import scheduler as sched_mod
from app.config import Settings
from app.scheduler import build_scheduler

LONDON = ZoneInfo("Europe/London")


def fake_app(digest=False):
    app = MagicMock()
    app.state.channels.subscribed.side_effect = lambda event: [object()] if digest else []
    app.state.settings = Settings(_env_file=None)
    app.state.http = AsyncMock()
    return app


def test_check_job_registered(settings):
    s = build_scheduler(fake_app(), settings)
    job = s.get_job("check")
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == settings.check_interval_hours * 3600
    assert job.max_instances == 1
    assert job.coalesce is True


def test_digest_job_absent_when_no_channel_gets_the_digest(settings):
    s = build_scheduler(fake_app(), settings)
    assert s.get_job("digest") is None


def test_digest_job_next_fire_time(settings):
    sch = build_scheduler(fake_app(digest=True), settings)
    job = sch.get_job("digest")
    assert isinstance(job.trigger, CronTrigger)
    assert job.max_instances == 1 and job.coalesce is True
    now = datetime(2026, 10, 17, 12, 0, tzinfo=LONDON)
    assert job.trigger.get_next_fire_time(None, now) == datetime(2026, 10, 19, 8, 0, tzinfo=LONDON)


async def test_check_job_swallows_errors(monkeypatch, caplog):
    monkeypatch.setattr(sched_mod, "run_check", AsyncMock(side_effect=RuntimeError("boom")))
    await sched_mod.scheduled_check(fake_app())
    assert "boom" in caplog.text


async def test_digest_job_runs_check_then_sends(monkeypatch, settings):
    result = object()
    run = AsyncMock(return_value=result)
    monkeypatch.setattr(sched_mod, "run_check", run)
    app = fake_app()
    app.state.notifier.digest = AsyncMock(return_value="sent")
    await sched_mod.scheduled_digest(app)
    run.assert_awaited_once_with(app)
    app.state.notifier.digest.assert_awaited_once_with(result)


async def test_digest_job_swallows_send_errors(monkeypatch, settings, caplog):
    monkeypatch.setattr(sched_mod, "run_check", AsyncMock(return_value=object()))
    app = fake_app()
    app.state.notifier.digest = AsyncMock(side_effect=RuntimeError("accesskey=TOPSECRET"))
    await sched_mod.scheduled_digest(app)
    assert "RuntimeError" in caplog.text and "TOPSECRET" not in caplog.text


async def test_digest_job_logs_check_failure_traceback(monkeypatch, settings, caplog):
    monkeypatch.setattr(sched_mod, "run_check", AsyncMock(side_effect=RuntimeError("down")))
    app = fake_app()
    app.state.settings = settings
    await sched_mod.scheduled_digest(app)
    assert "down" in caplog.text and "Traceback" in caplog.text


SATURDAY_NOON = datetime(2026, 10, 17, 12, 0, tzinfo=LONDON)


def _next(schedule):
    trigger = sched_mod.digest_trigger(schedule, LONDON)
    return trigger.get_next_fire_time(None, SATURDAY_NOON)


def test_numeric_weekdays_follow_cron():
    # cron: 0/7 = Sunday, 1 = Monday … 6 = Saturday (APScheduler alone reads 1 as Tuesday)
    assert _next("0 8 * * 1") == datetime(2026, 10, 19, 8, 0, tzinfo=LONDON)  # Monday
    assert _next("0 8 * * 0") == datetime(2026, 10, 18, 8, 0, tzinfo=LONDON)  # Sunday
    assert _next("0 8 * * 7") == datetime(2026, 10, 18, 8, 0, tzinfo=LONDON)
    assert _next("0 8 * * 2-3") == datetime(2026, 10, 20, 8, 0, tzinfo=LONDON)  # Tuesday
    assert _next("0 8 * * 3,5") == datetime(2026, 10, 21, 8, 0, tzinfo=LONDON)  # Wednesday
    assert _next("0 13 * * 6") == datetime(2026, 10, 17, 13, 0, tzinfo=LONDON)  # Saturday
    assert _next("0 8 * * MON") == datetime(2026, 10, 19, 8, 0, tzinfo=LONDON)


def test_weekday_translation():
    t = sched_mod._cron_weekdays
    assert t("1") == "mon"
    assert t("1-5") == "mon,tue,wed,thu,fri"
    assert t("0,6") == "sun,sat"
    assert t("5-7") == "sun,fri,sat"
    assert t("*/2") == "sun,tue,thu,sat"
    assert t("mon-fri") == "mon-fri"
    assert t("*") == "*"


async def test_reschedule_changes_interval_and_adds_digest(settings):
    app = fake_app()
    app.state.settings = settings.model_copy(update={"check_interval_hours": 6})
    sch = build_scheduler(app, app.state.settings)
    sch.start(paused=True)
    app.state.scheduler = sch
    assert sch.get_job("digest") is None

    app.state.settings = settings.model_copy(update={"check_interval_hours": 2})
    app.state.channels.subscribed.side_effect = lambda event: [object()]  # a channel opts in
    sched_mod.reschedule(app)
    job = sch.get_job("check")
    assert job.trigger.interval.total_seconds() == 2 * 3600
    assert job.next_run_time > datetime.now(job.next_run_time.tzinfo) + timedelta(hours=1)
    assert sch.get_job("digest") is not None

    app.state.channels.subscribed.side_effect = lambda event: []
    sched_mod.reschedule(app)
    assert sch.get_job("digest") is None
    sch.shutdown(wait=False)


def test_sessions_purge_job_registered(settings):
    app = fake_app()
    s = build_scheduler(app, settings)
    job = s.get_job("sessions-purge")
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == 3600
    job.func(*job.args)
    app.state.sessions.purge_expired.assert_called_once_with()
    app.state.users.purge_failures.assert_called_once_with(timedelta(days=1))


async def test_reschedule_keeps_check_timing_unless_interval_or_tz_changed(settings):
    app = fake_app()
    app.state.settings = settings.model_copy(update={"check_interval_hours": 6})
    sch = build_scheduler(app, app.state.settings)
    sch.start(paused=True)
    app.state.scheduler = sch
    before = sch.get_job("check").next_run_time

    # A save that changes something else (e.g. the server name) must not push the check out
    app.state.settings = app.state.settings.model_copy(update={"server_name": "renamed"})
    sched_mod.reschedule(app)
    assert sch.get_job("check").next_run_time == before

    app.state.settings = app.state.settings.model_copy(update={"check_interval_hours": 2})
    sched_mod.reschedule(app)
    changed = sch.get_job("check")
    assert changed.trigger.interval.total_seconds() == 2 * 3600
    assert changed.next_run_time != before

    app.state.settings = app.state.settings.model_copy(update={"tz": "Asia/Tokyo"})
    sched_mod.reschedule(app)
    assert str(sch.get_job("check").trigger.timezone) == "Asia/Tokyo"
    sch.shutdown(wait=False)


async def test_reschedule_digest_trigger_carries_new_schedule_and_tz(settings):
    app = fake_app(digest=True)
    app.state.settings = settings
    sch = build_scheduler(app, app.state.settings)
    sch.start(paused=True)
    app.state.scheduler = sch

    app.state.settings = settings.model_copy(update={
        "digest_schedule": "30 9 * * FRI", "tz": "Asia/Tokyo"})
    sched_mod.reschedule(app)
    trigger = sch.get_job("digest").trigger
    assert str(trigger.timezone) == "Asia/Tokyo"
    fields = {f.name: str(f) for f in trigger.fields}
    assert (fields["minute"], fields["hour"], fields["day_of_week"]) == ("30", "9", "fri")
    sch.shutdown(wait=False)


async def test_first_check_is_now_with_check_on_startup(settings):
    sch = build_scheduler(fake_app(), settings.model_copy(update={"check_on_startup": True}))
    sch.start(paused=True)
    job_time = sch.get_job("check").next_run_time
    assert abs((job_time - datetime.now(job_time.tzinfo)).total_seconds()) < 5


async def test_first_check_one_interval_away_without_check_on_startup(settings):
    s = settings.model_copy(update={"check_on_startup": False, "check_interval_hours": 6})
    sch = build_scheduler(fake_app(), s)
    sch.start(paused=True)
    when = sch.get_job("check").next_run_time
    delta = (when - datetime.now(when.tzinfo)).total_seconds()
    assert 6 * 3600 - 5 < delta <= 6 * 3600


async def test_random_delay_sleeps_in_range_before_check(monkeypatch):
    order = []
    sleeps = []

    async def fake_sleep(n):
        sleeps.append(n)
        order.append("sleep")

    async def fake_check(app):
        order.append("check")
        return object()

    monkeypatch.setattr(sched_mod, "_sleep", fake_sleep)
    monkeypatch.setattr(sched_mod, "_uniform", lambda a, b: b / 2)
    monkeypatch.setattr(sched_mod, "run_check", fake_check)
    monkeypatch.setattr(sched_mod, "check_failed", lambda r: False)
    app = fake_app()
    app.state.settings = Settings(_env_file=None).model_copy(update={"random_delay_minutes": 10})
    await sched_mod.scheduled_check(app)
    assert sleeps == [300] and order == ["sleep", "check"]


async def test_no_sleep_without_delay(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(sched_mod, "_sleep", sleep)
    monkeypatch.setattr(sched_mod, "run_check", AsyncMock(return_value=object()))
    monkeypatch.setattr(sched_mod, "check_failed", lambda r: False)
    await sched_mod.scheduled_check(fake_app())
    sleep.assert_not_awaited()


@respx.mock
async def test_heartbeat_success_and_fail_urls():
    ok = respx.get("https://hc.example/ping/abc").mock(return_value=httpx.Response(200))
    bad = respx.get("https://hc.example/ping/abc/fail").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        await sched_mod.heartbeat(http, "https://hc.example/ping/abc", False)
        await sched_mod.heartbeat(http, "https://hc.example/ping/abc/", True)
        await sched_mod.heartbeat(http, None, False)
    assert ok.call_count == 1 and bad.call_count == 1


@respx.mock
async def test_heartbeat_swallows_timeout_and_logs_type_only(caplog):
    respx.get("https://hc.example/p").mock(side_effect=httpx.ReadTimeout("secret-token-xyz"))
    async with httpx.AsyncClient() as http:
        await sched_mod.heartbeat(http, "https://hc.example/p", False)
    assert "Heartbeat failed (ReadTimeout)" in caplog.text
    assert "secret-token-xyz" not in caplog.text


async def test_heartbeat_uses_ten_second_timeout():
    http = AsyncMock()
    await sched_mod.heartbeat(http, "https://hc.example/p", False)
    assert http.get.await_args.kwargs["timeout"] == 10


async def _heartbeat_after(monkeypatch, run, failed_rule=None):
    beat = AsyncMock()
    monkeypatch.setattr(sched_mod, "heartbeat", beat)
    monkeypatch.setattr(sched_mod, "run_check", run)
    app = fake_app()
    app.state.settings = Settings(_env_file=None).model_copy(update={"heartbeat_url": "https://hc.example/p"})
    if failed_rule is not None:
        monkeypatch.setattr(sched_mod, "check_failed", failed_rule)
    await sched_mod.scheduled_check(app)
    return beat, app


async def test_scheduled_check_pings_success(monkeypatch):
    beat, app = await _heartbeat_after(monkeypatch, AsyncMock(return_value=object()), lambda r: False)
    beat.assert_awaited_once_with(app.state.http, "https://hc.example/p", False)


async def test_scheduled_check_pings_fail_on_failed_result(monkeypatch):
    beat, app = await _heartbeat_after(monkeypatch, AsyncMock(return_value=object()), lambda r: True)
    beat.assert_awaited_once_with(app.state.http, "https://hc.example/p", True)


async def test_scheduled_check_pings_fail_when_check_raises(monkeypatch):
    beat, app = await _heartbeat_after(monkeypatch, AsyncMock(side_effect=RuntimeError("x")))
    beat.assert_awaited_once_with(app.state.http, "https://hc.example/p", True)


async def test_scheduled_check_survives_heartbeat_errors(monkeypatch):
    monkeypatch.setattr(sched_mod, "run_check", AsyncMock(return_value=object()))
    monkeypatch.setattr(sched_mod, "check_failed", lambda r: False)
    app = fake_app()
    app.state.settings = Settings(_env_file=None).model_copy(update={"heartbeat_url": "https://hc.example/p"})
    app.state.http.get = AsyncMock(side_effect=httpx.ConnectError("nope"))
    await sched_mod.scheduled_check(app)  # must not raise


async def test_run_check_alone_sends_no_heartbeat(monkeypatch):
    # Check now calls run_check directly, so only scheduled_check pings.
    import inspect
    from app import main
    assert "heartbeat" not in inspect.getsource(main.run_check)
