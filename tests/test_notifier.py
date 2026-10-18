"""Notifier: sending, failure isolation, check alerts, update de-duplication, digest."""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import httpx
import pytest

from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status
from app.inventory import ContainerInfo
from app.keys import SecretBox
from app.notify import notifier as notifier_mod
from app.notify.channels import Channel, ChannelStore
from app.notify.notifier import Notifier, check_failed, current_updates
from app.notify.providers import NO_ANSWER, REJECTED
from app.store import Store
from notify_fakes import FakeProvider, failing, providers_for

NOW = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)
ACS = {"connection_string": "endpoint=https://x;accesskey=TOPSECRET", "sender": "from@x.com"}


def cs(name, st=Status.UP_TO_DATE, image="nginx:1.25", **kw):
    c = ContainerInfo(
        id=name, name=name, image_ref=image, image_id="sha256:img",
        running_repo_digests=["nginx@sha256:aaaaaaaaaaaaaaaaaaaa"],
        local_tag_image_id="sha256:img", project="site", working_dir="/x",
        config_files=[], service=name, state="running", is_self=False,
    )
    return ContainerStatus(container=c, status=st, **kw)


def result(*containers, mailcow=None):
    return CheckResult(NOW, list(containers), mailcow)


class Rig:
    def __init__(self, settings, tmp_path, provider=None):
        self.store = Store(str(tmp_path / "d.db"))
        self.channels = ChannelStore(self.store, SecretBox(b"k" * 32), lambda: settings)
        self.provider = provider or FakeProvider()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        self.bg: set = set()
        self.notifier = Notifier(self.store, self.channels, lambda: settings, self.http,
                                 providers_for(self.provider), background=self.bg,
                                 kind_labels={"build": "Image update"})

    def channel(self, name="Ops", events=("digest",), recipients=("a@x.com",)):
        return self.channels.save(
            Channel("", name, "acs", {}, list(recipients), set(events)), ACS, "admin")

    async def settle(self):
        await asyncio.gather(*list(self.bg), return_exceptions=True)

    @property
    def subjects(self):
        return [m.subject for _, m in self.provider.sent]


@pytest.fixture
def rig(settings, tmp_path):
    return Rig(settings, tmp_path)


# ---- sending ----

async def test_send_records_success_per_channel(rig):
    ch = rig.channel(events=("job_failed",))
    n = await rig.notifier.send("job_failed", ("S", "plain", "<p>h</p>"))
    assert n == 1
    config, msg = rig.provider.sent[0]
    assert (msg.subject, msg.recipients) == ("S", ("a@x.com",)) and config == ACS
    last = rig.notifier.last_result(ch.id)
    assert last["ok"] is True and last["reason"] is None and last["event"] == "job_failed"
    assert last["at"]


async def test_only_subscribed_channels_get_it(rig):
    rig.channel("A", events=("job_failed",))
    rig.channel("B", events=("digest",))
    assert await rig.notifier.send("job_failed", ("S", "p", "h")) == 1
    assert await rig.notifier.send("updates_found", ("S", "p", "h")) == 0


async def test_provider_error_is_recorded_and_never_raised(settings, tmp_path, caplog):
    rig = Rig(settings, tmp_path, failing())
    ch = rig.channel(events=("job_failed",))
    with caplog.at_level(logging.WARNING):
        assert await rig.notifier.send("job_failed", ("S", "p", "h")) == 0
    last = rig.notifier.last_result(ch.id)
    assert last["ok"] is False and last["reason"] == REJECTED
    assert "Notification to Ops failed (NotifyError)" in caplog.text


async def test_unexpected_exception_gets_a_fixed_reason_and_logs_type_only(
        settings, tmp_path, caplog):
    rig = Rig(settings, tmp_path, FakeProvider(fail=RuntimeError("accesskey=TOPSECRET")))
    ch = rig.channel(events=("job_failed",))
    with caplog.at_level(logging.DEBUG):
        assert await rig.notifier.send("job_failed", ("S", "p", "h")) == 0
    assert rig.notifier.last_result(ch.id)["reason"] == REJECTED
    assert "Notification to Ops failed (RuntimeError)" in caplog.text
    assert "TOPSECRET" not in caplog.text
    assert "TOPSECRET" not in json.dumps(rig.notifier.last_result(ch.id))


async def test_hanging_provider_times_out(settings, tmp_path, monkeypatch):
    rig = Rig(settings, tmp_path, FakeProvider(hang=True))
    monkeypatch.setattr(notifier_mod, "SEND_TIMEOUT", 0.05)
    ch = rig.channel(events=("job_failed",))
    started = time.monotonic()
    assert await rig.notifier.send("job_failed", ("S", "p", "h")) == 0
    assert time.monotonic() - started < 2
    assert rig.notifier.last_result(ch.id)["reason"] == NO_ANSWER


async def test_fire_returns_at_once_and_runs_in_background(settings, tmp_path):
    rig = Rig(settings, tmp_path, FakeProvider(hang=True))
    rig.channel(events=("job_failed",))
    rig.notifier.fire("job_failed", ("S", "p", "h"))  # sync: nothing to wait for
    assert len(rig.bg) == 1
    for task in list(rig.bg):
        task.cancel()
    await rig.settle()


async def test_fire_delivers(rig):
    rig.channel(events=("user_pending",))
    rig.notifier.user_pending("Alice", "alice@example.com")
    await rig.settle()
    assert "waiting for approval" in rig.subjects[0]
    assert "alice@example.com" in rig.provider.sent[0][1].plain


async def test_disabled_channel_is_skipped(rig):
    ch = rig.channel(events=("job_failed",))
    rig.channels.save(Channel(ch.id, ch.name, "acs", {}, ch.recipients, ch.events, False),
                      ACS, "admin")
    assert await rig.notifier.send("job_failed", ("S", "p", "h")) == 0


async def test_test_message_goes_to_one_channel_only(rig):
    a = rig.channel("A", events=())
    rig.channel("B", events=("digest",))
    assert await rig.notifier.test(a.id) is None
    assert len(rig.provider.sent) == 1 and "test notification" in rig.subjects[0]
    assert rig.notifier.last_result(a.id)["event"] == "test"
    assert await rig.notifier.test("nope") == "That channel no longer exists"


async def test_test_returns_fixed_reason(settings, tmp_path):
    rig = Rig(settings, tmp_path, failing("The email service refused the login"))
    ch = rig.channel(events=())
    assert await rig.notifier.test(ch.id) == "The email service refused the login"


# ---- failed-check rule ----

def test_check_failed_rule():
    assert check_failed(None) is True
    assert check_failed(result(cs("a", Status.ERROR), cs("b", Status.ERROR))) is True
    assert check_failed(result(cs("a", Status.ERROR), cs("b"))) is False
    assert check_failed(result()) is False
    assert check_failed(result(cs("a", Status.NOT_CHECKABLE))) is False
    # not-checkable containers don't count towards "all errored"
    assert check_failed(result(cs("a", Status.ERROR), cs("b", Status.NOT_CHECKABLE))) is True


async def test_alert_once_at_second_failure_then_one_recovery(rig):
    rig.channel(events=("check_failing",))
    bad = result(cs("a", Status.ERROR))
    good = result(cs("a"))
    n = rig.notifier
    n.after_check(bad, False)
    await rig.settle()
    assert rig.subjects == []
    n.after_check(None, True)  # raised / Docker unreachable
    await rig.settle()
    assert len(rig.subjects) == 1 and "failing" in rig.subjects[0]
    n.after_check(bad, False)  # third: no second alert
    await rig.settle()
    assert len(rig.subjects) == 1
    n.after_check(good, False)
    await rig.settle()
    assert len(rig.subjects) == 2 and "recovered" in rig.subjects[1]
    n.after_check(good, False)
    await rig.settle()
    assert len(rig.subjects) == 2


async def test_one_failure_then_good_sends_nothing_and_resets(rig):
    rig.channel(events=("check_failing",))
    n = rig.notifier
    n.after_check(result(cs("a", Status.ERROR)), False)
    n.after_check(result(cs("a")), False)
    n.after_check(result(cs("a", Status.ERROR)), False)  # counter restarted: still one failure
    await rig.settle()
    assert rig.subjects == []


async def test_single_image_error_is_not_a_failed_check(rig):
    rig.channel(events=("check_failing",))
    for _ in range(4):
        rig.notifier.after_check(result(cs("a", Status.ERROR), cs("b")), False)
    await rig.settle()
    assert rig.subjects == []


# ---- updates found ----

def test_current_updates_keys_and_signatures():
    r = result(
        cs("web", Status.NEWER_BUILD, remote_digest="sha256:new"),
        cs("db", minor_update="1.26", major_update="2.0"),
        cs("old", Status.NOT_CHECKABLE),
        mailcow=MailcowStatus(update_available=True, current_version="a", latest_release="b"),
    )
    assert current_updates(r) == {
        "web:build": "sha256:new", "db:minor": "1.26", "db:major": "2.0", "mailcow": "b"}
    assert current_updates(result(mailcow=MailcowStatus(error="x", update_available=True))) == {}


async def test_updates_found_notifies_once_per_new_update(rig):
    rig.channel(events=("updates_found",))
    n = rig.notifier
    first = result(cs("web", Status.NEWER_BUILD, remote_digest="d1"))
    n.after_check(first, False)
    await rig.settle()
    assert len(rig.subjects) == 1 and "1 new update available" in rig.subjects[0]
    n.after_check(first, False)  # same result again
    await rig.settle()
    assert len(rig.subjects) == 1
    n.after_check(result(cs("web", Status.NEWER_BUILD, remote_digest="d2")), False)
    await rig.settle()  # a newer digest for the same container
    assert len(rig.subjects) == 2
    n.after_check(result(cs("web")), False)  # fixed...
    n.after_check(result(cs("web", Status.NEWER_BUILD, remote_digest="d2")), False)
    await rig.settle()  # ...and back again
    assert len(rig.subjects) == 3


async def test_only_new_items_are_listed(rig):
    rig.channel(events=("updates_found",))
    n = rig.notifier
    n.after_check(result(cs("a", minor_update="1.1")), False)
    n.after_check(result(cs("a", minor_update="1.1"), cs("b", minor_update="2.1")), False)
    await rig.settle()
    second = rig.provider.sent[1][1].plain
    assert "b" in second and "2.1" in second and "1.1" not in second


async def test_errored_lookup_does_not_forget_a_notified_update(rig):
    rig.channel(events=("updates_found",))
    n = rig.notifier
    n.after_check(result(cs("a", minor_update="1.1"), cs("b")), False)
    n.after_check(result(cs("a", Status.ERROR), cs("b")), False)  # a's lookup failed
    n.after_check(result(cs("a", minor_update="1.1"), cs("b")), False)
    await rig.settle()
    assert len(rig.subjects) == 1


async def test_updates_tracked_even_when_nobody_subscribes_yet(rig):
    n = rig.notifier
    n.after_check(result(cs("a", minor_update="1.1")), False)
    rig.channel(events=("updates_found",))  # subscribes later: the old update stays quiet
    n.after_check(result(cs("a", minor_update="1.1")), False)
    await rig.settle()
    assert rig.subjects == []


# ---- jobs, users, digest ----

async def test_job_events_go_to_subscribed_channels_only(rig):
    rig.channel("Fail", events=("job_failed",))
    rig.channel("Win", events=("job_succeeded",))
    rig.notifier.job_finished("build", "web", "failed", "me@x.com", 7)
    await rig.settle()
    assert len(rig.provider.sent) == 1 and "failed" in rig.subjects[0]
    assert "https://dash.example.com/jobs/7" in rig.provider.sent[0][1].plain
    assert "Image update" in rig.provider.sent[0][1].plain
    rig.notifier.job_finished("build", "web", "succeeded", "me@x.com", 8)
    rig.notifier.job_finished("build", "web", "running", "me@x.com", 9)  # ignored
    await rig.settle()
    assert len(rig.provider.sent) == 2


async def test_digest_outcomes(rig, settings, tmp_path):
    assert await rig.notifier.digest(result(cs("a"))) == "skipped – no channel gets the digest"
    rig.channel(events=("digest",))
    assert await rig.notifier.digest(result(cs("a", minor_update="1.1"))) == "sent"
    assert "1 update available" in rig.subjects[0]
    (tmp_path / "q").mkdir()
    quiet = Rig(settings.model_copy(update={"digest_when_none": False}), tmp_path / "q")
    quiet.channel(events=("digest",))
    assert await quiet.notifier.digest(result(cs("a"))) == "skipped – nothing to report"


async def test_digest_reports_failure(settings, tmp_path):
    rig = Rig(settings, tmp_path, failing())
    rig.channel(events=("digest",))
    assert await rig.notifier.digest(result(cs("a", minor_update="1.1"))) == "failed"


# ---- upgraded install: env channel only ----

async def test_env_only_install_only_ever_sends_the_digest(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ACS_CONNECTION_STRING", ACS["connection_string"])
    monkeypatch.setenv("DIGEST_FROM", "env@x.com")
    monkeypatch.setenv("DIGEST_TO", "e@x.com")
    from app.config import Settings

    env = Settings(_env_file=None)
    rig = Rig(env, tmp_path)
    assert [c.id for c in rig.channels.list()] == ["env"]
    n = rig.notifier
    n.after_check(result(cs("a", minor_update="1.1")), False)
    n.after_check(None, True)
    n.after_check(None, True)
    n.after_check(result(cs("a")), False)
    n.job_finished("build", "web", "failed", "me", 1)
    n.job_finished("build", "web", "succeeded", "me", 2)
    n.user_pending("Alice", "alice@example.com")
    await rig.settle()
    assert rig.subjects == []
    assert await n.digest(result(cs("a", minor_update="1.1"))) == "sent"
    assert len(rig.subjects) == 1 and rig.provider.sent[0][1].recipients == ("e@x.com",)
