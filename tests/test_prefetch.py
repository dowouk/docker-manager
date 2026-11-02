import asyncio
import logging
from datetime import datetime, timezone

import docker.errors

from app.checker import CheckResult, ContainerStatus, Status
from app.inventory import ContainerInfo
from app.prefetch import (
    Prefetcher, State, Target, compose_editable, image_platform, image_present, targets,
)


def make_info(**kw) -> ContainerInfo:
    base = dict(
        id="abc", name="web", image_ref="nginx:1.0", image_id="sha256:x",
        running_repo_digests=[], local_tag_image_id=None, project="proj",
        working_dir="/docker/proj", config_files=["/docker/proj/a.yml"],
        service="web", state="running", is_self=False,
    )
    base.update(kw)
    return ContainerInfo(**base)


def cs_for(info, status=Status.NEWER_BUILD, **kw) -> ContainerStatus:
    kw.setdefault("remote_digest", "sha256:new" if status == Status.NEWER_BUILD else None)
    return ContainerStatus(container=info, status=status, **kw)


def result(*statuses) -> CheckResult:
    return CheckResult(checked_at=datetime(2026, 11, 2), containers=list(statuses))


def yes(cs, tag):
    return True


def no(cs, tag):
    return False


def test_newer_build_is_a_target():
    info = make_info()
    out = targets(result(cs_for(info)), [info], yes)
    assert out == [Target(key="nginx:1.0", kind="build", info=info, ref="nginx:1.0", remote_digest="sha256:new")]


def test_shared_tag_with_skipped_sibling_is_not_a_target():
    a, b = make_info(id="a"), make_info(id="b", name="web2", service="web2")
    visible = result(cs_for(a), cs_for(b, Status.UP_TO_DATE))  # b's update was skipped
    assert targets(visible, [a, b], yes) == []


def test_shared_tag_with_unwatched_sibling_is_not_a_target():
    a, b = make_info(id="a"), make_info(id="b", name="web2", service="web2")
    visible = result(cs_for(a), cs_for(b, Status.NOT_WATCHED))
    assert targets(visible, [a, b], yes) == []


def test_shared_tag_with_mailcow_container_is_not_a_target():
    a = make_info(id="a")
    mail = make_info(id="m", name="mailcow-web", project="mailcowdockerized", service="nginx-mailcow")
    assert targets(result(cs_for(a)), [a, mail], yes) == []


def test_shared_tag_all_watched_gives_one_target():
    a, b = make_info(id="a"), make_info(id="b", name="web2", service="web2")
    out = targets(result(cs_for(a), cs_for(b)), [a, b], yes)
    assert len(out) == 1
    assert out[0].info is a and out[0].kind == "build"


def test_minor_version_is_a_target_with_new_ref():
    info = make_info(image_ref="postgres:16.2")
    out = targets(result(cs_for(info, Status.UP_TO_DATE, minor_update="16.4")), [info], yes)
    assert out == [Target(key="postgres:16.4", kind="version", info=info, ref="postgres:16.4", remote_digest=None)]


def test_minor_version_shared_by_two_containers_gives_one_target():
    a = make_info(id="a", image_ref="postgres:16.2")
    b = make_info(id="b", image_ref="postgres:16.2", service="db2")
    visible = result(
        cs_for(a, Status.UP_TO_DATE, minor_update="16.4"),
        cs_for(b, Status.UP_TO_DATE, minor_update="16.4"),
    )
    assert len(targets(visible, [a, b], yes)) == 1


def test_version_set_by_variable_is_not_a_target():
    info = make_info(image_ref="postgres:16.2")
    visible = result(cs_for(info, Status.UP_TO_DATE, minor_update="16.4"))
    assert targets(visible, [info], no) == []


def test_major_only_dashboard_and_non_compose_are_never_targets():
    major = make_info(id="1", image_ref="redis:6.0", name="r")
    me = make_info(id="2", image_ref="app:1", name="me", is_self=True)
    plain = make_info(id="3", image_ref="x:1", name="x", service=None)
    manual = make_info(id="4", image_ref="y:1", name="y")
    visible = result(
        cs_for(major, Status.UP_TO_DATE, major_update="7.0"),
        cs_for(me),
        cs_for(plain),
        cs_for(manual, updatable=False, minor_update="1.2"),
    )
    assert targets(visible, [major, me, plain, manual], yes) == []


def test_compose_editable_literal_tag_yes_variable_no(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text("services:\n  db:\n    image: postgres:16.2\n  cache:\n    image: redis:${REDIS_TAG}\n")
    db = cs_for(make_info(image_ref="postgres:16.2", service="db", config_files=[str(f)]), Status.UP_TO_DATE)
    cache = cs_for(make_info(image_ref="redis:7.0", service="cache", config_files=[str(f)]), Status.UP_TO_DATE)
    assert compose_editable(db, "16.4") is True
    assert compose_editable(cache, "7.2") is False


def test_compose_editable_missing_file_is_false(tmp_path):
    cs = cs_for(make_info(config_files=[str(tmp_path / "gone.yml")]), Status.UP_TO_DATE)
    assert compose_editable(cs, "1.1") is False


def _ab(b_status):
    a = make_info(id="a", image_ref="postgres:16.2")
    b = make_info(id="b", image_ref="postgres:16.4", service="db2")
    visible = result(cs_for(a, Status.UP_TO_DATE, minor_update="16.4"), cs_for(b, b_status))
    return targets(visible, [a, b], yes)


def test_version_ref_already_used_by_watched_newer_build_gives_one_build_target():
    out = _ab(Status.NEWER_BUILD)
    assert [(t.kind, t.key) for t in out] == [("build", "postgres:16.4")]
    assert out[0].remote_digest == "sha256:new"


def test_version_ref_used_by_container_with_skipped_build_is_not_a_target():
    assert _ab(Status.UP_TO_DATE) == []


def test_build_target_wins_regardless_of_container_order():
    a = make_info(id="a", image_ref="postgres:16.2")
    b = make_info(id="b", image_ref="postgres:16.4", service="db2")
    visible = result(cs_for(b), cs_for(a, Status.UP_TO_DATE, minor_update="16.4"))
    assert [t.kind for t in targets(visible, [a, b], yes)] == ["build"]


# --- Downloading in the background ---

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def build_target(name, digest="sha256:new"):
    info = make_info(id=name, name=name, service=name, image_ref=f"{name}:latest")
    return Target(f"{name}:latest", "build", info, f"{name}:latest", digest)


def version_target(ref="postgres:16.4", image_id="sha256:pg"):
    info = make_info(id="db", name="db", service="db", image_ref="postgres:16.2", image_id=image_id)
    return Target(ref, "version", info, ref, None)


class FakeRun:
    def __init__(self, fail=(), block=()):
        self.commands = []
        self.fail = set(fail)
        self.block = set(block)  # services whose pull waits for `release`
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def __call__(self, cmd, cwd):
        self.commands.append((cmd, cwd))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.started.set()
            if any(s in cmd for s in self.block):
                await self.release.wait()
            await asyncio.sleep(0)
            return (1, "error") if any(s in cmd for s in self.fail) else (0, "")
        finally:
            self.active -= 1


def make_prefetcher(run, present=lambda t: False, platform=lambda info: None):
    return Prefetcher(run, present, lambda: NOW, platform=platform)


def pulled(run):
    return [cmd[-1] for cmd, _ in run.commands]


async def test_round_pulls_each_target_once_in_order():
    run = FakeRun()
    p = make_prefetcher(run)
    a, b = build_target("a"), build_target("b")
    p.request([a, b])
    await p.wait_idle()
    assert run.commands == [
        (["docker", "compose", "-p", "proj", "-f", "/docker/proj/a.yml", "pull", "a"], "/docker/proj"),
        (["docker", "compose", "-p", "proj", "-f", "/docker/proj/a.yml", "pull", "b"], "/docker/proj"),
    ]
    assert p.state("a:latest") == State("ready", NOW)
    assert p.state("b:latest") == State("ready", NOW)


async def test_present_image_is_ready_without_pulling():
    run = FakeRun()
    a, b = build_target("a"), build_target("b")
    p = make_prefetcher(run, present=lambda t: t.key == "a:latest")
    p.request([a, b])
    await p.wait_idle()
    assert pulled(run) == ["b"]
    assert p.state("a:latest").status == "ready"


async def test_failed_pull_is_failed_and_round_continues(caplog):
    run = FakeRun(fail={"a"})
    p = make_prefetcher(run)
    with caplog.at_level(logging.WARNING, logger="app.prefetch"):
        p.request([build_target("a"), build_target("b")])
        await p.wait_idle()
    assert pulled(run) == ["a", "b"]
    assert p.state("a:latest") == State("failed", NOW)
    assert p.state("b:latest").status == "ready"
    assert "a:latest" in caplog.text
    assert "error" not in caplog.text  # the output is not kept


async def test_pull_that_raises_is_failed_and_round_continues():
    async def run(cmd, cwd):
        if "a" in cmd:
            raise FileNotFoundError("docker")
        return 0, ""

    p = make_prefetcher(run)
    p.request([build_target("a"), build_target("b")])
    await p.wait_idle()
    assert p.state("a:latest").status == "failed"
    assert p.state("b:latest").status == "ready"


async def test_downloading_while_the_pull_runs():
    run = FakeRun(block={"a"})
    p = make_prefetcher(run)
    p.request([build_target("a")])
    await run.started.wait()
    assert p.state("a:latest") == State("downloading", NOW)
    run.release.set()
    await p.wait_idle()
    assert p.state("a:latest").status == "ready"


async def test_new_request_during_round_waits_and_replaces_queued():
    run = FakeRun(block={"a"})
    p = make_prefetcher(run)
    p.request([build_target("a")])
    await run.started.wait()
    p.request([build_target("b")])
    p.request([build_target("c")])
    await asyncio.sleep(0.01)
    assert pulled(run) == ["a"]  # nothing else starts while A is pulling
    run.release.set()
    await p.wait_idle()
    assert pulled(run) == ["a", "c"]
    assert run.max_active == 1
    assert p.state("b:latest") is None


async def test_request_after_a_round_finishes_starts_another():
    run = FakeRun()
    p = make_prefetcher(run)
    p.request([build_target("a")])
    await p.wait_idle()
    p.request([build_target("b")])
    await p.wait_idle()
    assert pulled(run) == ["a", "b"]


async def test_version_pull_uses_running_image_platform():
    run = FakeRun()
    seen = []

    def platform(info):
        seen.append(info.image_id)
        return "linux/arm64/v8"

    p = make_prefetcher(run, platform=platform)
    p.request([version_target()])
    await p.wait_idle()
    assert run.commands == [(["docker", "pull", "--platform", "linux/arm64/v8", "postgres:16.4"], None)]
    assert seen == ["sha256:pg"]
    assert p.state("postgres:16.4").status == "ready"


async def test_version_pull_without_platform_omits_it():
    run = FakeRun()
    p = make_prefetcher(run)
    p.request([version_target()])
    await p.wait_idle()
    assert run.commands == [(["docker", "pull", "postgres:16.4"], None)]


async def test_states_for_dropped_targets_are_forgotten():
    run = FakeRun(fail={"b"})
    p = make_prefetcher(run)
    p.request([build_target("a"), build_target("b")])
    await p.wait_idle()
    p.request([build_target("a")])
    await p.wait_idle()
    assert p.state("a:latest").status == "ready"
    assert p.state("b:latest") is None


async def test_ready_keeps_the_time_it_was_downloaded():
    times = iter([NOW, NOW, datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)])
    present = {"a": False}
    p = Prefetcher(FakeRun(), lambda t: present["a"], lambda: next(times))
    p.request([build_target("a")])
    await p.wait_idle()
    present["a"] = True
    p.request([build_target("a")])
    await p.wait_idle()
    assert p.state("a:latest") == State("ready", NOW)


async def test_round_runs_in_the_spawned_task():
    spawned = []

    def spawn(coro):
        task = asyncio.create_task(coro)
        spawned.append(task)
        return task

    p = Prefetcher(FakeRun(), lambda t: False, lambda: NOW, spawn=spawn)
    p.request([build_target("a")])
    await p.wait_idle()
    assert len(spawned) == 1 and spawned[0].done()


# --- What Docker already has ---

class FakeImage:
    def __init__(self, attrs):
        self.attrs = attrs


class FakeImages:
    def __init__(self, images=None, error=None):
        self.images = images or {}
        self.error = error

    def get(self, ref):
        if self.error:
            raise self.error
        if ref not in self.images:
            raise docker.errors.ImageNotFound(ref)
        return FakeImage(self.images[ref])


class FakeClient:
    def __init__(self, **kw):
        self.images = FakeImages(**kw)


def test_build_present_when_a_repo_digest_matches():
    client = FakeClient(images={"a:latest": {"RepoDigests": ["a@sha256:old", "a@sha256:new"]}})
    assert image_present(client, build_target("a")) is True
    assert image_present(client, build_target("a", digest="sha256:other")) is False
    assert image_present(client, build_target("a", digest=None)) is False


def test_build_not_present_when_the_tag_is_missing():
    assert image_present(FakeClient(), build_target("a")) is False


def test_version_present_when_the_tag_exists():
    assert image_present(FakeClient(images={"postgres:16.4": {}}), version_target()) is True
    assert image_present(FakeClient(), version_target()) is False


def test_present_is_false_on_any_docker_error():
    client = FakeClient(error=docker.errors.APIError("daemon down"))
    assert image_present(client, build_target("a")) is False
    assert image_present(client, version_target()) is False


def test_platform_from_the_running_image():
    info = version_target().info
    client = FakeClient(images={"sha256:pg": {"Os": "linux", "Architecture": "arm64", "Variant": "v8"}})
    assert image_platform(client, info) == "linux/arm64/v8"
    client = FakeClient(images={"sha256:pg": {"Os": "linux", "Architecture": "amd64"}})
    assert image_platform(client, info) == "linux/amd64"


def test_platform_unknown_when_it_cannot_be_read():
    info = version_target().info
    assert image_platform(FakeClient(), info) is None
    assert image_platform(FakeClient(images={"sha256:pg": {"Os": "linux"}}), info) is None
    assert image_platform(FakeClient(error=docker.errors.APIError("down")), info) is None
