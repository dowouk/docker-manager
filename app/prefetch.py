"""Work out which updates can be downloaded ahead of time, and download them in the background."""
import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import docker.errors

from app.checker import CheckResult, ContainerStatus, Status
from app.compose_edit import EditRefused, plan_tag_change
from app.imageref import parse_image_ref
from app.inventory import ContainerInfo
from app.mailcow import run_capture
from app.updaters import compose_cmd

log = logging.getLogger(__name__)

# Large images can take a long time to download on a slow line.
PULL_TIMEOUT = 1800


@dataclass(frozen=True)
class Target:
    key: str  # state key: image_ref for a build, the new ref ("postgres:16.4") for a version
    kind: Literal["build", "version"]
    info: ContainerInfo  # one container to run the pull for
    ref: str  # what to pull: image_ref (build) or repo:new tag (version)
    remote_digest: str | None  # build only: the digest the check found


def compose_editable(cs: ContainerStatus, tag: str) -> bool:
    """Whether the compose file sets the image as a plain tag we can change to `tag`."""
    info = cs.container
    new_ref = parse_image_ref(info.image_ref).with_tag(tag)
    try:
        plan_tag_change(info.config_files, info.service, new_ref)
    except (EditRefused, OSError):
        return False
    return True


def _build_shared_ok(image_ref: str, containers: list[ContainerInfo], by_id: dict[str, ContainerStatus]) -> bool:
    """Every container using this tag must be watched and waiting for the same newer build."""
    for c in containers:
        if c.image_ref != image_ref:
            continue
        cs = by_id.get(c.id)
        if cs is None or cs.status != Status.NEWER_BUILD:
            return False
    return True


def targets(
    visible: CheckResult,
    containers: list[ContainerInfo],
    editable: Callable[[ContainerStatus, str], bool],
) -> list[Target]:
    by_id = {cs.container.id: cs for cs in visible.containers}
    found: dict[str, Target] = {}
    for cs in visible.containers:
        info = cs.container
        try:
            if info.is_self or not cs.updatable or not info.service:
                continue
            if cs.status == Status.NEWER_BUILD:
                key = info.image_ref
                if _build_shared_ok(key, containers, by_id):
                    # A build target wins over a version target with the same key: it carries the digest.
                    if key not in found or found[key].kind == "version":
                        found[key] = Target(key, "build", info, info.image_ref, cs.remote_digest)
            if cs.minor_update:
                new_ref = parse_image_ref(info.image_ref).with_tag(cs.minor_update)
                if new_ref in found:
                    continue
                # Pulling the new version also moves the tag any container already on it uses.
                if any(c.image_ref == new_ref for c in containers) and not _build_shared_ok(
                        new_ref, containers, by_id):
                    continue
                if editable(cs, cs.minor_update):
                    found[new_ref] = Target(new_ref, "version", info, new_ref, None)
        except Exception:  # noqa: BLE001 – one odd project mustn't stop every download
            log.warning("Couldn't work out what to download ahead for %s", info.name, exc_info=True)
    return list(found.values())


@dataclass
class State:
    status: Literal["downloading", "ready", "failed"]
    at: datetime  # when the status was set


async def run_pull(cmd: list[str], cwd: str | None) -> tuple[int, str]:
    """Run a pull as Update does (same environment, so saved registry logins work)."""
    return await run_capture(cmd, PULL_TIMEOUT, cwd=cwd)


def image_present(client, target: Target) -> bool:
    """Whether Docker already has what the target would download."""
    try:
        if target.kind == "build":
            if not target.remote_digest:
                return False
            digests = client.images.get(target.info.image_ref).attrs.get("RepoDigests") or []
            return any(d.rsplit("@", 1)[-1] == target.remote_digest for d in digests)
        client.images.get(target.ref)
        return True
    except docker.errors.DockerException:
        return False


def image_platform(client, info: ContainerInfo) -> str | None:
    """os/arch[/variant] of the image the container runs, or None if it can't be read."""
    try:
        attrs = client.images.get(info.image_id).attrs
    except docker.errors.DockerException:
        return None
    os_, arch, variant = attrs.get("Os"), attrs.get("Architecture"), attrs.get("Variant")
    if not os_ or not arch:
        return None
    return "/".join([os_, arch, variant] if variant else [os_, arch])


class Prefetcher:
    """Downloads targets one at a time, outside the JobRunner, so Update is never kept waiting."""

    def __init__(
        self,
        run: Callable[[list[str], str | None], Awaitable[tuple[int, str]]],
        present: Callable[[Target], bool],
        now: Callable[[], datetime],
        *,
        platform: Callable[[ContainerInfo], str | None] = lambda info: None,
        spawn: Callable[[Coroutine], asyncio.Task] = asyncio.create_task,
    ) -> None:
        self._run = run
        self._present = present
        self._platform = platform
        self._now = now
        self._spawn = spawn
        self._states: dict[str, State] = {}
        self._queued: list[Target] | None = None
        self._task: asyncio.Task | None = None

    def request(self, targets: list[Target]) -> None:
        """Download these next. Replaces any round still waiting; a running round stops once
        its current pull finishes."""
        self._queued = list(targets)
        if self._task is None:
            self._task = self._spawn(self._work())

    def state(self, key: str) -> State | None:
        return self._states.get(key)

    async def wait_idle(self) -> None:
        while self._task is not None:
            await asyncio.wait([self._task])

    async def _work(self) -> None:
        try:
            while self._queued is not None:
                batch, self._queued = self._queued, None
                await self._round(batch)
        finally:
            # No await between the last check of _queued and this, so no request is lost.
            self._task = None

    async def _round(self, batch: list[Target]) -> None:
        keys = {t.key for t in batch}
        self._states = {k: v for k, v in self._states.items() if k in keys}
        for target in batch:
            if self._queued is not None:
                break  # a newer request replaces the rest of this round
            try:
                await self._fetch(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Couldn't download %s ahead", target.ref, exc_info=True)
                self._states[target.key] = State("failed", self._now())

    async def _fetch(self, target: Target) -> None:
        if await asyncio.to_thread(self._present, target):
            old = self._states.get(target.key)
            if old is None or old.status != "ready":
                self._states[target.key] = State("ready", self._now())
            return
        self._states[target.key] = State("downloading", self._now())
        info = target.info
        if target.kind == "build":
            cmd, cwd = compose_cmd(info, "pull", info.service), info.working_dir
        else:
            platform = await asyncio.to_thread(self._platform, info)
            cmd = ["docker", "pull", *(["--platform", platform] if platform else []), target.ref]
            cwd = None
        rc, _ = await self._run(cmd, cwd)
        if rc == 0:
            self._states[target.key] = State("ready", self._now())
        else:
            log.warning("Couldn't download %s ahead (exit code %s)", target.ref, rc)
            self._states[target.key] = State("failed", self._now())
