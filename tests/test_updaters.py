import asyncio
import os
import shlex
from datetime import datetime

import pytest

from app.compose_edit import plan_tag_change
from app.inventory import ContainerInfo
from app.updaters import (
    UpdateFailed,
    apply_config,
    compose_cmd,
    control_service,
    follow_self_update,
    remove_finished_helpers,
    start_self_update,
    update_build,
    update_version,
    wait_healthy,
)


def make_info(**kw) -> ContainerInfo:
    base = dict(
        id="abc", name="web", image_ref="nginx:1.0", image_id="sha256:x",
        running_repo_digests=[], local_tag_image_id=None, project="proj",
        working_dir="/docker/proj", config_files=["/docker/proj/a.yml", "/docker/proj/b.yml"],
        service="web", state="running", is_self=False,
    )
    base.update(kw)
    return ContainerInfo(**base)


class FakeCtx:
    def __init__(self, results=None):
        self.results = list(results or [])  # (rc, output) per call; default (0, "")
        self.calls: list[tuple[list[str], str | None]] = []
        self.logs: list[str] = []
        self.last_output = ""

    def log(self, text):
        self.logs.append(text)

    async def run(self, cmd, cwd=None, timeout=1800, stdin_devnull=True):
        self.last_output = ""
        self.calls.append((cmd, cwd))
        rc, out = self.results.pop(0) if self.results else (0, "")
        self.last_output = out
        return rc

    @property
    def log_text(self):
        return "".join(self.logs)


def test_compose_cmd():
    assert compose_cmd(make_info(), "pull", "web") == [
        "docker", "compose", "-p", "proj", "-f", "/docker/proj/a.yml",
        "-f", "/docker/proj/b.yml", "pull", "web",
    ]
    assert "-f" not in compose_cmd(make_info(config_files=[]), "ps")


@pytest.mark.parametrize("kw", [{"project": None}, {"working_dir": None}])
async def test_not_started_by_compose(kw):
    ctx = FakeCtx()
    with pytest.raises(UpdateFailed, match="Not started by compose"):
        await update_build(ctx, make_info(**kw))
    assert ctx.calls == []


def test_compose_cmd_passes_env_files_before_subcommand(tmp_path):
    env1, env2 = tmp_path / ".env", tmp_path / "extra.env"
    env1.write_text("A=1\n")
    env2.write_text("B=2\n")
    info = make_info(env_files=[str(env1), str(env2)])
    assert compose_cmd(info, "pull", "web") == [
        "docker", "compose", "-p", "proj", "-f", "/docker/proj/a.yml",
        "-f", "/docker/proj/b.yml", "--env-file", str(env1), "--env-file", str(env2),
        "pull", "web",
    ]


async def test_missing_env_file_refuses_update(tmp_path):
    info = make_info(env_files=[str(tmp_path / "gone.env")])
    ctx = FakeCtx()
    with pytest.raises(UpdateFailed) as e:
        await update_build(ctx, info)
    assert str(e.value) == "Project uses an env file that isn't mounted – update by hand"
    assert ctx.calls == []


async def test_update_build_pull_then_up():
    ctx = FakeCtx()
    await update_build(ctx, make_info())
    assert [c for c, _ in ctx.calls] == [
        compose_cmd(make_info(), "pull", "web"),
        compose_cmd(make_info(), "up", "-d", "--no-deps", "web"),
    ]
    assert all(cwd == "/docker/proj" for _, cwd in ctx.calls)


@pytest.mark.parametrize("action", ["restart", "stop", "start"])
async def test_control_service_runs_one_compose_command(action):
    ctx = FakeCtx()
    await control_service(ctx, make_info(), action)
    assert ctx.calls == [(compose_cmd(make_info(), action, "web"), "/docker/proj")]


async def test_control_service_failure_names_the_action():
    ctx = FakeCtx([(1, "boom")])
    with pytest.raises(UpdateFailed, match=r"^Stop failed \(exit code 1\)$"):
        await control_service(ctx, make_info(), "stop")


@pytest.mark.parametrize("action, kw", [("down", {}), ("stop", {"service": None}),
                                        ("stop", {"project": None})])
async def test_control_service_refuses(action, kw):
    ctx = FakeCtx()
    with pytest.raises(UpdateFailed):
        await control_service(ctx, make_info(**kw), action)
    assert ctx.calls == []


async def test_update_build_whole_project_omits_service():
    ctx = FakeCtx()
    await update_build(ctx, make_info(), whole_project=True)
    assert [c[-1:] for c, _ in ctx.calls] == [["pull"], ["-d"]]


async def test_pull_rate_limited():
    ctx = FakeCtx([(1, "Error: TooManyRequests: you have reached")])
    with pytest.raises(UpdateFailed) as e:
        await update_build(ctx, make_info())
    assert str(e.value) == "Docker Hub rate limit reached – try again later"
    assert len(ctx.calls) == 1


async def test_pull_failure_generic():
    ctx = FakeCtx([(1, "boom")])
    with pytest.raises(UpdateFailed, match="Pull failed"):
        await update_build(ctx, make_info())


async def test_up_failure_rate_limit_text_variant():
    ctx = FakeCtx([(0, ""), (1, "Rate Limit exceeded")])
    with pytest.raises(UpdateFailed, match="rate limit reached"):
        await update_build(ctx, make_info())


@pytest.fixture
def compose_file(tmp_path):
    p = tmp_path / "docker-compose.yml"
    p.write_text("services:\n  web:\n    image: nginx:1.0\n")
    return p


def setup_version(compose_file):
    info = make_info(config_files=[str(compose_file)])
    change = plan_tag_change(info.config_files, "web", "nginx:1.1")
    return info, change


NOW = datetime(2026, 10, 17, 12, 0, 0)


async def test_version_update_success(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    ctx = FakeCtx()

    async def wait(i, t):
        return True

    await update_version(ctx, info, change, str(tmp_path / "bk"), wait, now=NOW)
    assert "nginx:1.1" in compose_file.read_text()
    assert [c for c, _ in ctx.calls] == [
        compose_cmd(info, "pull", "web"), compose_cmd(info, "up", "-d", "--no-deps", "web")]
    assert "nginx:1.1" in ctx.log_text
    backups = list((tmp_path / "bk" / "proj").iterdir())
    assert len(backups) == 1 and f"Backup saved to {backups[0]}" in ctx.log_text


async def test_version_pull_fails_restores(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    original = compose_file.read_text()
    ctx = FakeCtx([(1, "denied")])

    async def wait(i, t):
        raise AssertionError("must not wait")

    with pytest.raises(UpdateFailed):
        await update_version(ctx, info, change, str(tmp_path / "bk"), wait, now=NOW)
    assert compose_file.read_text() == original
    assert len(ctx.calls) == 1


async def test_version_pull_rate_limited_restores(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    original = compose_file.read_text()
    ctx = FakeCtx([(1, "toomanyrequests")])
    with pytest.raises(UpdateFailed, match="rate limit reached"):
        await update_version(ctx, info, change, str(tmp_path / "bk"), None, now=NOW)
    assert compose_file.read_text() == original


async def test_version_unhealthy_rolls_back(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    original = compose_file.read_text()
    ctx = FakeCtx()
    waited = []

    async def wait(i, t):
        waited.append(t)
        return False

    with pytest.raises(UpdateFailed, match="rolled back"):
        await update_version(ctx, info, change, str(tmp_path / "bk"), wait, health_timeout=7, now=NOW)
    assert compose_file.read_text() == original
    ups = [c for c, _ in ctx.calls if c[-4:] == ["up", "-d", "--no-deps", "web"]]
    assert len(ups) == 2
    assert waited == [7]
    assert "Rolled back to nginx:1.0" in ctx.log_text


async def test_version_up_fails_rolls_back(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    original = compose_file.read_text()
    ctx = FakeCtx([(0, ""), (1, "bad"), (0, "")])

    async def wait(i, t):
        raise AssertionError("must not wait")

    with pytest.raises(UpdateFailed, match="rolled back"):
        await update_version(ctx, info, change, str(tmp_path / "bk"), wait, now=NOW)
    assert compose_file.read_text() == original
    assert len(ctx.calls) == 3
    assert "Rolled back to nginx:1.0" in ctx.log_text


class Obj:
    def __init__(self, status, health=None, restarts=0, cid="c1"):
        self.id = cid
        self.attrs = {"State": {"Status": status}, "RestartCount": restarts}
        if health:
            self.attrs["State"]["Health"] = {"Status": health}

    def reload(self):
        pass


class StubClient:
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.filters = None
        self.containers = self

    def list(self, all=False, filters=None):
        self.filters = filters
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


async def test_wait_healthy_filters_and_succeeds_after_starting():
    c = StubClient([Obj("running", "starting")], [Obj("running", "healthy")])
    assert await wait_healthy(c, make_info(), 5, poll_interval=0, stable_window=0) is True
    assert c.filters == {"label": [
        "com.docker.compose.project=proj", "com.docker.compose.service=web",
        "com.docker.compose.oneoff=False"]}


async def test_wait_healthy_running_without_healthcheck():
    assert await wait_healthy(StubClient([Obj("running")]), make_info(), 5, poll_interval=0, stable_window=0)


async def test_wait_healthy_needs_a_stable_window():
    loop = asyncio.get_running_loop()
    t = loop.time()
    assert await wait_healthy(
        StubClient([Obj("running")]), make_info(), 5, poll_interval=0.01, stable_window=0.2
    ) is True
    assert loop.time() - t >= 0.2


async def test_wait_healthy_restart_count_change_resets_the_window():
    # Crash-looping between polls: always seen running, but RestartCount keeps rising.
    snaps = [[Obj("running", restarts=n)] for n in range(1000)]
    assert await wait_healthy(
        StubClient(*snaps), make_info(), 0.3, poll_interval=0.01, stable_window=0.1
    ) is False


async def test_wait_healthy_restart_then_stable_succeeds():
    c = StubClient([Obj("running", restarts=0)], [Obj("running", restarts=1)], [Obj("running", restarts=1)])
    assert await wait_healthy(c, make_info(), 5, poll_interval=0.01, stable_window=0.05) is True


async def test_wait_healthy_default_stable_window_is_20s():
    import inspect

    assert inspect.signature(wait_healthy).parameters["stable_window"].default == 20


@pytest.mark.parametrize("snap", [[Obj("exited")], [Obj("running", "unhealthy")], [Obj("dead")],
                                  [Obj("restarting")]])
async def test_wait_healthy_fails_early(snap):
    loop = asyncio.get_running_loop()
    t = loop.time()
    assert await wait_healthy(StubClient(snap), make_info(), 30, poll_interval=0) is False
    assert loop.time() - t < 5


async def test_wait_healthy_times_out():
    assert await wait_healthy(StubClient([Obj("running", "starting")]), make_info(), 0.05, poll_interval=0.01) is False


async def test_wait_healthy_no_container_times_out():
    assert await wait_healthy(StubClient([]), make_info(), 0.05, poll_interval=0.01) is False


def test_start_self_update_args():
    calls = []

    class C:
        containers = None

        def run(self, *a, **kw):
            calls.append((a, kw))

        def list(self, all=False, filters=None):
            return []

    c = C()
    c.containers = c
    info = make_info(image_ref="ghcr.io/o/dash:1", working_dir="/docker/my proj",
                     config_files=["/docker/my proj/c.yml"], service="dash")
    start_self_update(c, info, "/docker")
    (a, kw), = calls
    assert a == ("ghcr.io/o/dash:1",)
    # kept after it exits, so its output and exit code can be read
    assert "remove" not in kw and kw["detach"] is True
    assert kw["working_dir"] == "/docker/my proj"
    assert kw["labels"] == {"docker-manager.helper": "self-update"}
    assert kw["volumes"] == {
        "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
        "/docker/my proj": {"bind": "/docker/my proj", "mode": "rw"},
        "/docker": {"bind": "/docker", "mode": "rw"},
    }
    assert kw["entrypoint"] == ["sh", "-c"]
    # the image's /healthz HEALTHCHECK must not run in the short-lived helper
    assert kw["healthcheck"] == {"test": ["NONE"]}
    script, = kw["command"]
    first, second = script.split(" && ")
    assert shlex.split(first) == compose_cmd(info, "pull", "dash")
    assert shlex.split(second) == compose_cmd(info, "up", "-d", "--no-deps", "dash")


class RaisingCtx(FakeCtx):
    def __init__(self, raise_on, exc=None, **kw):
        super().__init__(**kw)
        self.raise_on = raise_on  # 1-based call numbers that raise
        self.exc = exc or OSError("no docker")

    async def run(self, cmd, cwd=None, timeout=1800, stdin_devnull=True):
        n = len(self.calls) + 1
        if n in self.raise_on:
            self.calls.append((cmd, cwd))
            raise self.exc
        return await super().run(cmd, cwd, timeout, stdin_devnull)


async def ok_wait(i, t):
    return True


async def test_oserror_on_pull_restores_and_raises_update_failed(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    original = compose_file.read_text()
    ctx = RaisingCtx({1})
    with pytest.raises(UpdateFailed, match="no docker") as e:
        await update_version(ctx, info, change, str(tmp_path / "bk"), ok_wait, now=NOW)
    assert "bk" in str(e.value)
    assert compose_file.read_text() == original


async def test_wait_raising_rolls_back(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    original = compose_file.read_text()
    ctx = FakeCtx()

    async def wait(i, t):
        raise RuntimeError("docker gone")

    with pytest.raises(UpdateFailed, match="docker gone.*rolled back"):
        await update_version(ctx, info, change, str(tmp_path / "bk"), wait, now=NOW)
    assert compose_file.read_text() == original
    assert len(ctx.calls) == 3


async def test_cancel_during_wait_restores_and_reraises(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    original = compose_file.read_text()
    ctx = FakeCtx()

    async def wait(i, t):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await update_version(ctx, info, change, str(tmp_path / "bk"), wait, now=NOW)
    assert compose_file.read_text() == original
    assert len(ctx.calls) == 2  # no rollback up on cancel


async def test_cancel_during_pull_restores_and_reraises(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    original = compose_file.read_text()
    ctx = RaisingCtx({1}, exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await update_version(ctx, info, change, str(tmp_path / "bk"), ok_wait, now=NOW)
    assert compose_file.read_text() == original


async def test_rollback_also_fails(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    ctx = FakeCtx([(0, ""), (0, ""), (1, "")])

    async def wait(i, t):
        return False

    with pytest.raises(UpdateFailed, match="rollback also failed.*by hand"):
        await update_version(ctx, info, change, str(tmp_path / "bk"), wait, now=NOW)
    assert "Rolled back" not in ctx.log_text


async def test_rollback_up_raising_is_reported(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    ctx = RaisingCtx({3})

    async def wait(i, t):
        return False

    with pytest.raises(UpdateFailed, match="rollback also failed"):
        await update_version(ctx, info, change, str(tmp_path / "bk"), wait, now=NOW)


async def test_restore_failure_names_backup(compose_file, tmp_path, monkeypatch):
    info, change = setup_version(compose_file)
    ctx = FakeCtx([(1, "denied")])

    def boom(change, path):
        raise PermissionError("read-only")

    monkeypatch.setattr("app.updaters.restore_backup", boom)
    with pytest.raises(UpdateFailed, match=r"restore .*bk.* by hand"):
        await update_version(ctx, info, change, str(tmp_path / "bk"), ok_wait, now=NOW)


async def test_version_refuses_unmounted_env_file_before_editing(compose_file, tmp_path):
    info, change = setup_version(compose_file)
    info = make_info(config_files=[str(compose_file)], env_files=[str(tmp_path / "gone.env")])
    original = compose_file.read_text()
    with pytest.raises(UpdateFailed, match="env file that isn't mounted"):
        await update_version(FakeCtx(), info, change, str(tmp_path / "bk"), ok_wait, now=NOW)
    assert compose_file.read_text() == original


def test_self_update_mounts_and_passes_env_files(tmp_path):
    env = tmp_path / "dash.env"
    env.write_text("A=1\n")
    calls = []

    class C:
        containers = None

        def run(self, *a, **kw):
            calls.append(kw)

        def list(self, all=False, filters=None):
            return []

    c = C()
    c.containers = c
    info = make_info(env_files=[str(env)], service="dash")
    start_self_update(c, info, "/docker")
    (kw,) = calls
    assert kw["volumes"][str(env)] == {"bind": str(env), "mode": "ro"}
    assert f"--env-file {env}" in kw["command"][0]


def test_self_update_refuses_unmounted_env_file(tmp_path):
    info = make_info(env_files=[str(tmp_path / "gone.env")], is_self=True)
    with pytest.raises(UpdateFailed, match="env file that isn't mounted"):
        start_self_update(object(), info, "/docker")


@pytest.mark.parametrize("kw", [{"project": None}, {"working_dir": None}, {"service": None}])
async def test_update_version_rejects_missing_fields_before_editing(compose_file, tmp_path, kw):
    info, change = setup_version(compose_file)
    info = make_info(config_files=[str(compose_file)], **kw)
    original = compose_file.read_text()
    ctx = FakeCtx()
    with pytest.raises(UpdateFailed, match="Not started by compose"):
        await update_version(ctx, info, change, str(tmp_path / "bk"), ok_wait, now=NOW)
    assert compose_file.read_text() == original
    assert ctx.calls == [] and not (tmp_path / "bk").exists()


class Helper:
    def __init__(self, chunks=(b"pulling\n", b"done\n"), code=0, block=None):
        self.chunks = list(chunks)
        self.code = code
        self.block = block  # threading.Event the log stream waits on
        self.removed = False

    def logs(self, stream=False, follow=False):
        assert stream and follow
        yield from self.chunks
        if self.block is not None:
            self.block.wait(5)

    def wait(self):
        return {"StatusCode": self.code}

    def remove(self, force=False):
        self.removed = True


async def test_follow_self_update_streams_log_and_fails_on_nonzero_exit():
    ctx = FakeCtx()
    helper = Helper(chunks=[b"pulling\n", b"Error: no space\n"], code=1)
    with pytest.raises(UpdateFailed, match=r"helper failed \(exit code 1\)"):
        await follow_self_update(ctx, helper, timeout=5, grace=0)
    assert "pulling\nError: no space\n" in ctx.log_text
    assert helper.removed


async def test_follow_self_update_nothing_to_restart_succeeds():
    ctx = FakeCtx()
    helper = Helper()
    await follow_self_update(ctx, helper, timeout=5, grace=0)
    assert "pulling\ndone\n" in ctx.log_text
    assert "Already up to date – nothing to restart" in ctx.log_text
    assert helper.removed


async def test_follow_self_update_times_out():
    import threading

    release = threading.Event()
    ctx = FakeCtx()
    helper = Helper(block=release)
    try:
        with pytest.raises(UpdateFailed, match="not restarted in time"):
            await follow_self_update(ctx, helper, timeout=0.1, grace=0)
    finally:
        release.set()
    assert not helper.removed


def test_start_self_update_removes_old_helpers():
    removed, seen = [], {}

    class Old:
        def remove(self, force=False):
            removed.append(force)

    class C:
        containers = None

        def list(self, all=False, filters=None):
            seen.update(all=all, filters=filters)
            return [Old()]

        def run(self, *a, **kw):
            return "helper"

    c = C()
    c.containers = c
    assert start_self_update(c, make_info(service="dash"), "/docker") == "helper"
    assert seen == {"all": True, "filters": {"label": ["docker-manager.helper=self-update"]}}
    assert removed == [True]


async def test_backup_timestamp_uses_configured_time_zone(compose_file, tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo

    import app.updaters as upd

    seen = []
    real = upd.apply_tag_change

    def spy(change, backup_dir, project, now):
        seen.append(now)
        return real(change, backup_dir, project, now)

    monkeypatch.setattr(upd, "apply_tag_change", spy)
    info, change = setup_version(compose_file)
    tz = ZoneInfo("Pacific/Kiritimati")
    await update_version(FakeCtx(), info, change, str(tmp_path / "bk"), ok_wait, tz=tz)
    (now,) = seen
    assert now.tzinfo is tz


def test_finished_helpers_removed_but_failed_ones_kept():
    removed, seen = [], {}

    class Helper:
        def __init__(self, name, code):
            self.name, self.attrs = name, {"State": {"ExitCode": code}}

        def remove(self, force=False):
            removed.append(self.name)

    class C:
        def list(self, all=False, filters=None):
            seen.update(all=all, filters=filters)
            return [Helper("ok", 0), Helper("failed", 1)]

    client = type("Client", (), {"containers": C()})()
    remove_finished_helpers(client)
    assert seen == {"all": True, "filters": {"label": ["docker-manager.helper=self-update"], "status": "exited"}}
    assert removed == ["ok"]


def test_finished_helper_cleanup_never_raises():
    class Broken:
        def list(self, **kw):
            raise RuntimeError("daemon down")

    remove_finished_helpers(type("Client", (), {"containers": Broken()})())


async def test_apply_config_recreates_the_whole_project():
    ctx = FakeCtx()
    await apply_config(ctx, make_info())
    assert ctx.calls == [(compose_cmd(make_info(), "up", "-d"), "/docker/proj")]


def test_start_self_update_runs_given_steps():
    calls = []

    class C:
        containers = None

        def run(self, *a, **kw):
            calls.append(kw)

        def list(self, all=False, filters=None):
            return []

    c = C()
    c.containers = c
    start_self_update(c, make_info(service="dash"), "/docker", [["up", "-d"]])
    assert calls[0]["command"] == [shlex.join(compose_cmd(make_info(service="dash"), "up", "-d"))]
