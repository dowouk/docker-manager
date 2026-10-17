import asyncio
import sys
import time

import pytest

from app.jobs import BusyError, JobContext, JobRunner
from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


async def wait_idle(runner: JobRunner) -> None:
    for _ in range(200):
        if not runner.busy:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("runner stayed busy")


async def test_output_appears_in_log_and_succeeds(store):
    runner = JobRunner(store)

    async def work(ctx: JobContext) -> None:
        assert await ctx.run(["/bin/sh", "-c", "echo a; echo err >&2"]) == 0

    job_id = runner.submit("update", "web", "me", work)
    assert runner.busy
    await wait_idle(runner)
    job = store.get_job(job_id)
    assert job.status == "succeeded"
    assert "$ /bin/sh -c echo a; echo err >&2\n" in job.log
    assert "a\n" in job.log and "err\n" in job.log


async def test_raise_marks_failed_and_logs_message(store):
    runner = JobRunner(store)

    async def work(ctx: JobContext) -> None:
        rc = await ctx.run(["/bin/sh", "-c", "exit 3"])
        assert rc == 3
        raise RuntimeError("boom")

    job_id = runner.submit("update", "web", "me", work)
    await wait_idle(runner)
    job = store.get_job(job_id)
    assert job.status == "failed"
    assert "boom" in job.log


async def test_second_submit_while_running_is_busy(store):
    runner = JobRunner(store)

    async def work(ctx: JobContext) -> None:
        await ctx.run(["/bin/sh", "-c", "sleep 0.3"])

    runner.submit("update", "a", "me", work)
    with pytest.raises(BusyError, match="Another update is running – try again when it finishes"):
        runner.submit("update", "b", "me", work)
    await wait_idle(runner)
    runner.submit("update", "c", "me", work)  # free again
    await wait_idle(runner)


async def test_timeout_kills_process(store):
    runner = JobRunner(store)
    result = []

    async def work(ctx: JobContext) -> None:
        result.append(await ctx.run(["/bin/sh", "-c", "sleep 30"], timeout=0.3))

    job_id = runner.submit("update", "web", "me", work)
    await wait_idle(runner)
    assert result == [-1]
    assert "Timed out after 0.3 s" in store.get_job(job_id).log


async def test_stdin_is_closed(store):
    runner = JobRunner(store)
    code = "import sys; print(repr(sys.stdin.read()))"

    async def work(ctx: JobContext) -> None:
        await ctx.run([sys.executable, "-c", code])

    job_id = runner.submit("update", "web", "me", work)
    await wait_idle(runner)
    assert "''\n" in store.get_job(job_id).log


async def test_cwd(store, tmp_path):
    runner = JobRunner(store)

    async def work(ctx: JobContext) -> None:
        await ctx.run(["/bin/pwd"], cwd=str(tmp_path))

    job_id = runner.submit("update", "web", "me", work)
    await wait_idle(runner)
    assert tmp_path.name in store.get_job(job_id).log


async def test_subscribe_mid_job_gets_earlier_then_later_lines(store):
    runner = JobRunner(store)

    async def work(ctx: JobContext) -> None:
        await ctx.run(["/bin/sh", "-c", "echo a; sleep 0.5; echo b"])

    job_id = runner.submit("update", "web", "me", work)
    await asyncio.sleep(0.25)
    chunks = [c async for c in runner.subscribe(job_id)]
    assert chunks[-1] == "__END__:succeeded"
    text = "".join(chunks[:-1])
    assert text == store.get_job(job_id).log
    assert text.splitlines()[1:] == ["a", "b"]  # no gaps, no duplicates


async def test_subscribe_finished_job_and_unknown(store):
    runner = JobRunner(store)

    async def work(ctx: JobContext) -> None:
        ctx.log("hello\n")

    job_id = runner.submit("update", "web", "me", work)
    await wait_idle(runner)
    chunks = [c async for c in runner.subscribe(job_id)]
    assert chunks == ["hello\n", "__END__:succeeded"]
    assert [c async for c in runner.subscribe(999)] == ["__END__:unknown"]


async def test_failed_status_in_end_marker(store):
    runner = JobRunner(store)

    async def work(ctx: JobContext) -> None:
        raise RuntimeError("nope")

    job_id = runner.submit("update", "web", "me", work)
    chunks = [c async for c in runner.subscribe(job_id)]
    assert chunks[-1] == "__END__:failed"


async def test_disconnecting_subscriber_does_not_stop_job(store):
    runner = JobRunner(store)

    async def work(ctx: JobContext) -> None:
        await ctx.run(["/bin/sh", "-c", "echo a; sleep 0.3; echo b"])

    job_id = runner.submit("update", "web", "me", work)
    gen = runner.subscribe(job_id)
    first = await anext(gen)
    assert first
    await gen.aclose()
    assert runner._subscribers[job_id] == []
    await wait_idle(runner)
    job = store.get_job(job_id)
    assert job.status == "succeeded"
    assert "b\n" in job.log
    assert runner._subscribers == {}


async def test_timeout_kills_grandchildren_promptly(store):
    runner = JobRunner(store)
    result = []

    async def work(ctx: JobContext) -> None:
        start = time.monotonic()
        result.append(await ctx.run(["/bin/sh", "-c", "sleep 5 & sleep 30"], timeout=0.5))
        result.append(time.monotonic() - start)

    runner.submit("update", "web", "me", work)
    await wait_idle(runner)
    assert result[0] == -1
    assert result[1] < 2.5


async def test_background_grandchild_after_clean_exit_is_not_a_timeout(store):
    runner = JobRunner(store)
    result = []

    async def work(ctx: JobContext) -> None:
        start = time.monotonic()
        result.append(await ctx.run(["/bin/sh", "-c", "echo hi; sleep 5 & exit 0"], timeout=3))
        result.append(time.monotonic() - start)

    job_id = runner.submit("update", "web", "me", work)
    await wait_idle(runner)
    assert result[0] == 0
    assert result[1] < 2.5
    assert "hi\n" in store.get_job(job_id).log


async def test_store_failure_does_not_leave_runner_busy(store, monkeypatch):
    runner = JobRunner(store)

    def boom(job_id, status):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "finish_job", boom)

    async def work(ctx: JobContext) -> None:
        pass

    job_id = runner.submit("update", "web", "me", work)
    chunks = [c async for c in runner.subscribe(job_id)]
    assert chunks[-1] == "__END__:succeeded"
    await asyncio.sleep(0)
    assert not runner.busy


async def test_last_output_is_per_run_and_excludes_command_echo():
    ctx = JobContext(1, lambda text: None)
    assert ctx.last_output == ""
    assert await ctx.run(["/bin/sh", "-c", "echo one; echo two >&2"]) == 0
    assert ctx.last_output == "one\ntwo\n"
    assert await ctx.run(["/bin/sh", "-c", "echo three"]) == 0
    assert ctx.last_output == "three\n"


def _leaky_env(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "top-secret")
    monkeypatch.setenv("SESSION_SECRET", "also-secret")
    monkeypatch.setenv("TZ", "Europe/London")
    monkeypatch.setenv("DATA_DIR", "/data")
    monkeypatch.setenv("SERVER_NAME", "server1")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("COMPOSE_PROGRESS", "plain")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("HOME", "/root")


def _assert_clean(output: str) -> None:
    names = {line.split("=", 1)[0] for line in output.splitlines() if "=" in line}
    for leaked in ("AZURE_CLIENT_SECRET", "SESSION_SECRET", "TZ", "DATA_DIR", "SERVER_NAME"):
        assert leaked not in names
    assert "top-secret" not in output
    for kept in ("PATH", "HOME", "DOCKER_HOST", "COMPOSE_PROGRESS", "LC_ALL"):
        assert kept in names


async def test_run_does_not_leak_dashboard_env(monkeypatch):
    _leaky_env(monkeypatch)
    ctx = JobContext(1, lambda _text: None)
    assert await ctx.run(["/bin/sh", "-c", "env"]) == 0
    _assert_clean(ctx.last_output)


def test_subprocess_env_allowlist(monkeypatch):
    from app.jobs import subprocess_env

    _leaky_env(monkeypatch)
    env = subprocess_env()
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert "AZURE_CLIENT_SECRET" not in env and "TZ" not in env


async def test_run_sets_pwd_to_cwd(tmp_path):
    ctx = JobContext(1, lambda text: None)
    # `env`, not a shell: a shell sets PWD itself, docker compose doesn't.
    assert await ctx.run(["/usr/bin/env"], cwd=str(tmp_path)) == 0
    assert f"PWD={tmp_path}\n" in ctx.last_output.splitlines(keepends=True)


@pytest.mark.parametrize("kind,final", [("update", "failed"), ("self", "succeeded")])
async def test_cancel_leaves_job_running_for_startup_classification(store, kind, final):
    runner = JobRunner(store)
    started = asyncio.Event()

    async def work(ctx: JobContext) -> None:
        ctx.log("working\n")
        started.set()
        await asyncio.sleep(3600)

    job_id = runner.submit(kind, "web", "me", work)
    await started.wait()
    gen = runner.subscribe(job_id)
    assert await anext(gen) == "working\n"
    task = runner._task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await anext(gen) == "Interrupted – the dashboard is stopping\n"
    assert await anext(gen) == "__END__:interrupted"
    assert not runner.busy and runner._subscribers == {}
    job = store.get_job(job_id)
    assert job.status == "running" and job.finished_at is None
    assert "Cancelled" not in job.log
    store.mark_interrupted_jobs()
    assert store.get_job(job_id).status == final
