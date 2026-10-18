import json
from datetime import timezone

import pytest

from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status, check_all
from app.inventory import ContainerInfo
from app.registry import NotFoundError, RateLimitError, RegistryError


class FakeRegistry:
    def __init__(self, digests=None, tags=None):
        self.digests = digests or {}
        self.tags = tags or {}
        self.head_calls = []
        self.tag_calls = []

    async def head_digest(self, ref):
        self.head_calls.append(ref.with_tag(ref.tag))
        value = self.digests[ref.with_tag(ref.tag)]
        if isinstance(value, Exception):
            raise value
        return value

    async def list_tags(self, ref):
        self.tag_calls.append(ref.with_tag(ref.tag))
        value = self.tags.get(ref.with_tag(ref.tag), [])
        if isinstance(value, Exception):
            raise value
        return value


def info(tmp_path, name="web", image="nginx:latest", digests=("nginx@sha256:old",), **kw):
    values = dict(
        id=name * 4, name=name, image_ref=image, image_id="sha256:img",
        running_repo_digests=list(digests), local_tag_image_id="sha256:img",
        project="site", working_dir=str(tmp_path), config_files=[], service=name,
        state="running", is_self=False,
    )
    values.update(kw)
    return ContainerInfo(**values)


async def run(containers, registry, settings, mailcow_check=None):
    return await check_all(containers, registry, settings, mailcow_check)


async def test_up_to_date(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:old"})
    result = await run([info(tmp_path, digests=["docker.io/library/nginx@sha256:old"])], reg, settings)
    cs = result.containers[0]
    assert cs.status == Status.UP_TO_DATE
    assert cs.remote_digest == "sha256:old"
    assert cs.updatable and cs.not_updatable_reason is None
    assert result.summary() == {"up_to_date": 1, "updates": 0, "major": 0, "errors": 0}


async def test_newer_build(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:new"})
    result = await run([info(tmp_path)], reg, settings)
    assert result.containers[0].status == Status.NEWER_BUILD
    assert result.summary()["updates"] == 1


async def test_pulled_not_recreated(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:new"})
    c = info(tmp_path, local_tag_image_id="sha256:other", local_tag_repo_digests=["nginx@sha256:new"])
    result = await run([c], reg, settings)
    assert result.containers[0].status == Status.PULLED_NOT_RECREATED
    assert result.summary()["updates"] == 1


async def test_local_tag_different_but_digest_not_remote_is_newer_build(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:new"})
    c = info(tmp_path, local_tag_image_id="sha256:other", local_tag_repo_digests=["nginx@sha256:x"])
    assert (await run([c], reg, settings)).containers[0].status == Status.NEWER_BUILD


async def test_locally_built_and_pinned_not_checkable(tmp_path, settings):
    reg = FakeRegistry()
    built = info(tmp_path, name="built", digests=[])
    pinned = info(tmp_path, name="pin", image="nginx@sha256:abc")
    result = await run([built, pinned], reg, settings)
    assert [c.status for c in result.containers] == [Status.NOT_CHECKABLE] * 2
    assert result.containers[0].message == "Image built locally"
    assert result.containers[1].message == "Pinned by digest"
    assert reg.head_calls == []
    # neutral: neither errors nor up to date
    assert result.summary() == {"up_to_date": 0, "updates": 0, "major": 0, "errors": 0}


async def test_one_registry_failure_does_not_stop_others(tmp_path, settings):
    reg = FakeRegistry({"bad:1": RegistryError("boom"), "nginx:latest": "sha256:old"})
    bad = info(tmp_path, name="bad", image="bad:1", digests=["bad@sha256:a"])
    good = info(tmp_path)
    result = await run([bad, good], reg, settings)
    assert result.containers[0].status == Status.ERROR
    assert result.containers[0].message == "boom"
    assert result.containers[0].minor_update is None
    assert result.containers[1].status == Status.UP_TO_DATE


async def test_image_not_on_any_registry_is_neutral(tmp_path, settings):
    msg = "Not found on Docker Hub – a local or private image can't be checked"
    reg = FakeRegistry({"readdy-mcp:latest": NotFoundError(msg)})
    local = info(tmp_path, name="readdy", image="readdy-mcp:latest", digests=["readdy-mcp@sha256:a"])
    result = await run([local], reg, settings)
    assert result.containers[0].status == Status.NOT_CHECKABLE
    assert result.containers[0].message == msg
    assert result.summary()["errors"] == 0


async def test_rate_limit_message_verbatim(tmp_path, settings):
    msg = "Docker Hub rate limit reached – try again later"
    reg = FakeRegistry({"nginx:latest": RateLimitError(msg)})
    result = await run([info(tmp_path)], reg, settings)
    assert result.containers[0].status == Status.ERROR
    assert result.containers[0].message == msg


async def test_shared_image_hits_registry_once(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:old"})
    result = await run([info(tmp_path, name="a"), info(tmp_path, name="b")], reg, settings)
    assert reg.head_calls == ["nginx:latest"]
    assert len(result.containers) == 2


async def test_minor_and_major_populated(tmp_path, settings):
    reg = FakeRegistry(
        {"postgres:16.2": "sha256:old"},
        {"postgres:16.2": ["16.2", "16.3", "16.4", "17.1", "latest"]},
    )
    c = info(tmp_path, image="postgres:16.2", digests=["postgres@sha256:old"])
    result = await run([c], reg, settings)
    cs = result.containers[0]
    assert cs.status == Status.UP_TO_DATE
    assert cs.minor_update == "16.4"
    assert cs.major_update == "17.1"
    assert result.summary() == {"up_to_date": 0, "updates": 1, "major": 1, "errors": 0}
    assert reg.tag_calls == ["postgres:16.2"]


async def test_non_version_tag_skips_tag_listing(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:old"})
    await run([info(tmp_path)], reg, settings)
    assert reg.tag_calls == []


async def test_tag_list_failure_keeps_digest_result(tmp_path, settings):
    reg = FakeRegistry({"postgres:16.2": "sha256:new"}, {"postgres:16.2": RegistryError("tags down")})
    c = info(tmp_path, image="postgres:16.2", digests=["postgres@sha256:old"])
    cs = (await run([c], reg, settings)).containers[0]
    assert cs.status == Status.NEWER_BUILD
    assert "tags down" in cs.message
    assert cs.minor_update is None


async def test_mailcow_collapsed_and_checked_once(tmp_path, settings):
    mc_dir = tmp_path / "mailcow"
    mc_dir.mkdir()
    settings.mailcow_dir = str(mc_dir)
    calls = []

    async def mailcow_check():
        calls.append(1)
        return MailcowStatus(True, "2024-01", "2024-02", None)

    reg = FakeRegistry({"nginx:latest": "sha256:old"})
    containers = [
        info(tmp_path, name="web"),
        info(tmp_path, name="m1", project="mailcowdockerized", working_dir=str(mc_dir)),
        info(tmp_path, name="m2", project="other", working_dir=str(mc_dir)),
    ]
    result = await run(containers, reg, settings, mailcow_check)
    assert [c.container.name for c in result.containers] == ["web"]
    assert result.mailcow.update_available is True
    assert len(calls) == 1
    assert result.summary()["updates"] == 1


async def test_mailcow_not_called_without_containers_or_dir(tmp_path, settings):
    calls = []

    async def mailcow_check():
        calls.append(1)
        return MailcowStatus(False, None, None, None)

    reg = FakeRegistry({"nginx:latest": "sha256:old"})
    result = await run([info(tmp_path)], reg, settings, mailcow_check)
    assert result.mailcow is None
    assert calls == []


async def test_mailcow_containers_without_dir_show_error(tmp_path, settings):
    calls = []

    async def mailcow_check():
        calls.append(1)

    missing = str(tmp_path / "missing")
    settings.mailcow_dir = missing
    mc = info(tmp_path, project="mailcowdockerized")
    result = await run([mc], FakeRegistry({}), settings, mailcow_check)
    assert calls == [] and result.containers == []
    assert result.mailcow.error == f"Mailcow folder not mounted (MAILCOW_DIR={missing})"
    assert result.mailcow.update_available is None
    assert result.summary()["errors"] == 1


async def test_mailcow_error_counts_as_error(tmp_path, settings):
    settings.mailcow_dir = str(tmp_path)
    async def mailcow_check():
        return MailcowStatus(None, None, None, "exit 1")
    mc = info(tmp_path, project="mailcowdockerized")
    result = await run([mc], FakeRegistry({}), settings, mailcow_check)
    assert result.summary()["errors"] == 1


async def test_updatable_rules(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:old"})
    missing = info(tmp_path, name="a", working_dir=str(tmp_path / "nope"))
    plain = info(tmp_path, name="b", project=None, working_dir=None)
    stopped = info(tmp_path, name="c", state="paused")
    exited = info(tmp_path, name="d", state="exited")
    result = await run([missing, plain, stopped, exited], reg, settings)
    got = [(c.updatable, c.not_updatable_reason) for c in result.containers]
    assert got == [
        (False, "Project files not mounted"),
        (False, "Not started by compose"),
        (False, "Container is paused"),
        (True, None),
    ]


async def test_self_container_is_updatable(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:old"})
    result = await run([info(tmp_path, is_self=True)], reg, settings)
    assert result.containers[0].updatable


async def test_round_trip(tmp_path, settings):
    reg = FakeRegistry(
        {"postgres:16.2": "sha256:new", "nginx:latest": RegistryError("x")},
        {"postgres:16.2": ["16.3", "17.0"]},
    )
    containers = [
        info(tmp_path, image="postgres:16.2", digests=["postgres@sha256:old"],
             local_tag_repo_digests=["postgres@sha256:z"]),
        info(tmp_path, name="n"),
    ]
    mc = info(tmp_path, name="m", project="mailcowdockerized")
    mc_dir = tmp_path / "mailcow"
    mc_dir.mkdir()
    settings.mailcow_dir = str(mc_dir)

    async def mailcow_check():
        return MailcowStatus(False, "a", "b", None)

    result = await run(containers + [mc], reg, settings, mailcow_check)
    data = json.loads(json.dumps(result.to_dict()))
    restored = CheckResult.from_dict(data)
    assert restored == result
    assert restored.checked_at.tzinfo is not None
    assert restored.checked_at.utcoffset() == timezone.utc.utcoffset(None)
    assert isinstance(restored.containers[0], ContainerStatus)
    assert restored.containers[0].status is Status.NEWER_BUILD
