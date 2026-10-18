"""Compose update actions: run inside jobs against the owner's live servers."""

import asyncio
import codecs
import contextlib
import os
import shlex
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, tzinfo

from app.compose_edit import TagChange, apply_tag_change, restore_backup
from app.inventory import HELPER_LABEL, ContainerInfo
from app.jobs import JobContext
from app.logfilter import ProgressFilter

RATE_LIMIT_MESSAGE = "Docker Hub rate limit reached – try again later"
ENV_FILE_MESSAGE = "Project uses an env file that isn't mounted – update by hand"
_LABEL_PROJECT = "com.docker.compose.project"
_LABEL_SERVICE = "com.docker.compose.service"
_HELPER_LABEL = HELPER_LABEL
NOTHING_TO_RESTART = "Already up to date – nothing to restart"


class UpdateFailed(Exception):
    pass


def _require_compose(info: ContainerInfo) -> None:
    if not info.project or not info.working_dir:
        raise UpdateFailed("Not started by compose")
    # Started with --env-file: without the same files, variables would come out blank.
    for path in info.env_files:
        if not (os.path.isfile(path) and os.access(path, os.R_OK)):
            raise UpdateFailed(ENV_FILE_MESSAGE)


def compose_cmd(info: ContainerInfo, *args: str) -> list[str]:
    _require_compose(info)
    cmd = ["docker", "compose", "-p", info.project]
    for f in info.config_files:
        cmd += ["-f", f]
    for f in info.env_files:
        cmd += ["--env-file", f]
    return [*cmd, *args]


def _is_rate_limited(output: str) -> bool:
    low = output.lower()
    return "toomanyrequests" in low or "rate limit" in low


async def _run(ctx: JobContext, info: ContainerInfo, what: str, *args: str) -> None:
    rc = await ctx.run(compose_cmd(info, *args), cwd=info.working_dir)
    if rc != 0:
        if _is_rate_limited(ctx.last_output):
            raise UpdateFailed(RATE_LIMIT_MESSAGE)
        raise UpdateFailed(f"{what} failed (exit code {rc})")


def _target(info: ContainerInfo, whole_project: bool) -> list[str]:
    return [] if whole_project or not info.service else [info.service]


def _up(target: list[str]) -> list[str]:
    # A single-service update must not recreate the services it depends on.
    return ["up", "-d", "--no-deps", *target] if target else ["up", "-d"]


async def update_build(ctx: JobContext, info: ContainerInfo, whole_project: bool = False) -> None:
    _require_compose(info)
    target = _target(info, whole_project)
    await _run(ctx, info, "Pull", "pull", *target)
    await _run(ctx, info, "Start", *_up(target))


def _restore(ctx: JobContext, change: TagChange, backup: str) -> None:
    try:
        restore_backup(change, backup)
    except OSError as exc:
        raise UpdateFailed(
            f"Update failed and the compose file could not be restored ({exc})"
            f" – restore {backup} by hand"
        ) from exc
    ctx.log("Restored the compose file from backup\n")


def _restore_on_cancel(ctx: JobContext, change: TagChange, backup: str) -> None:
    try:
        _restore(ctx, change, backup)
    except UpdateFailed as exc:
        ctx.log(f"{exc}\n")


async def update_version(
    ctx: JobContext,
    info: ContainerInfo,
    change: TagChange,
    backup_dir: str,
    wait: Callable[[ContainerInfo, float], Awaitable[bool]],
    health_timeout: float = 300,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> None:
    _require_compose(info)
    if not info.service:
        raise UpdateFailed("Not started by compose")
    target = _target(info, False)
    ctx.log(change.diff.rstrip("\n") + "\n")
    backup = apply_tag_change(change, backup_dir, info.project, now or datetime.now(tz))
    ctx.log(f"Backup saved to {backup}\n")

    try:
        await _run(ctx, info, "Pull", "pull", *target)
    except asyncio.CancelledError:
        _restore_on_cancel(ctx, change, backup)
        raise
    except Exception as exc:  # noqa: BLE001 - always restore, whatever went wrong
        _restore(ctx, change, backup)
        if isinstance(exc, UpdateFailed):
            raise
        raise UpdateFailed(
            f"Update failed ({exc}); the compose file was restored (backup at {backup})"
        ) from exc

    failure: Exception | None = None
    try:
        await _run(ctx, info, "Start", *_up(target))
        if not await wait(info, health_timeout):
            failure = UpdateFailed("The service did not become healthy")
    except asyncio.CancelledError:
        _restore_on_cancel(ctx, change, backup)
        raise
    except Exception as exc:  # noqa: BLE001
        failure = exc
    if failure is None:
        return

    _restore(ctx, change, backup)
    try:
        rc = await ctx.run(compose_cmd(info, *_up(target)), cwd=info.working_dir)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"Rollback could not run: {exc}\n")
        rc = -1
    if rc == 0:
        ctx.log(f"Rolled back to {info.image_ref}\n")
        raise UpdateFailed(f"Update failed ({failure}) and was rolled back")
    raise UpdateFailed(
        f"Update failed ({failure}) and the rollback also failed"
        f" – check the server by hand (compose file backup at {backup})"
    )


_FAILED_STATES = ("exited", "dead", "restarting")


def _container_state(c) -> tuple[str, str | None, int]:
    c.reload()
    state = c.attrs.get("State") or {}
    health = (state.get("Health") or {}).get("Status")
    return state.get("Status", ""), health, int(c.attrs.get("RestartCount") or 0)


def _poll(client, info: ContainerInfo) -> tuple[bool | None, tuple]:
    """(True = all ready / False = definitely failed / None = keep waiting, restart counts)."""
    containers = client.containers.list(
        all=True,
        filters={
            "label": [
                f"{_LABEL_PROJECT}={info.project}",
                f"{_LABEL_SERVICE}={info.service}",
                "com.docker.compose.oneoff=False",
            ]
        },
    )
    if not containers:
        return None, ()
    ready = True
    restarts = []
    for c in containers:
        status, health, restart_count = _container_state(c)
        if status in _FAILED_STATES or health == "unhealthy":
            return False, ()
        if status != "running" or (health is not None and health != "healthy"):
            ready = False
        restarts.append((c.id, restart_count))
    return (True if ready else None), tuple(sorted(restarts))


async def wait_healthy(
    client,
    info: ContainerInfo,
    timeout: float,
    poll_interval: float = 2.0,
    stable_window: float = 20,
) -> bool:
    """Healthy = running (and healthy, if it has a healthcheck) with the same
    containers and restart counts for `stable_window` seconds. A crash-looping
    container is seen as `restarting` or as a rising restart count."""
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    seen: tuple | None = None
    while True:
        ready, restarts = await asyncio.to_thread(_poll, client, info)
        if ready is False:
            return False
        now = time.monotonic()
        if ready:
            if stable_since is None or restarts != seen:
                stable_since, seen = now, restarts
            if now - stable_since >= stable_window:
                return True
        else:
            stable_since = seen = None
        if now >= deadline:
            return False
        await asyncio.sleep(poll_interval)


def _remove_old_helpers(client) -> None:
    """Best effort: helpers are kept after exiting so their output can be read."""
    try:
        old = client.containers.list(all=True, filters={"label": [f"{_HELPER_LABEL}=self-update"]})
        for c in old:
            c.remove(force=True)
    except Exception:  # noqa: BLE001 - tidying up must never stop an update
        pass


def remove_finished_helpers(client) -> None:
    """Remove self-update helpers that finished successfully.

    A successful self-update replaces the dashboard, and the old one is stopped
    before it can remove its helper, so the helper would otherwise stay until the
    next self-update. Helpers that failed are kept so their output can be read on
    the server. Best effort; blocking Docker SDK calls.
    """
    try:
        finished = client.containers.list(
            all=True, filters={"label": [f"{_HELPER_LABEL}=self-update"], "status": "exited"})
        for c in finished:
            if (c.attrs.get("State") or {}).get("ExitCode") == 0:
                c.remove()
    except Exception:  # noqa: BLE001 - tidying up must never stop a check
        pass


def start_self_update(client, info: ContainerInfo, compose_root: str):
    """Start a helper container that pulls and recreates this dashboard; returns it.

    Blocking Docker SDK call; callers on the event loop should use asyncio.to_thread.
    """
    _require_compose(info)
    if not info.service:
        raise UpdateFailed("Not started by compose")
    script = " && ".join(
        shlex.join(compose_cmd(info, *args)) for args in (("pull", info.service), _up([info.service]))
    )
    _remove_old_helpers(client)
    sock = "/var/run/docker.sock"
    return client.containers.run(
        info.image_ref,
        entrypoint=["sh", "-c"],
        command=[script],
        detach=True,
        working_dir=info.working_dir,
        volumes={
            sock: {"bind": sock, "mode": "rw"},
            info.working_dir: {"bind": info.working_dir, "mode": "rw"},
            compose_root: {"bind": compose_root, "mode": "rw"},
            **{path: {"bind": path, "mode": "ro"} for path in info.env_files},
        },
        labels={_HELPER_LABEL: "self-update"},
        # Same image as the dashboard, so it would inherit the /healthz HEALTHCHECK.
        healthcheck={"test": ["NONE"]},
    )


def _watch_helper(helper, loop: asyncio.AbstractEventLoop, log, done: asyncio.Future) -> None:
    """Thread body: stream the helper's output into the job log, then report its exit code."""
    def post(fn, *args) -> bool:
        try:
            loop.call_soon_threadsafe(fn, *args)
            return True
        except RuntimeError:  # the event loop has closed: the dashboard is stopping
            return False

    def settle(fn, value) -> None:
        if not done.done():
            fn(value)

    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        progress = ProgressFilter()
        for chunk in helper.logs(stream=True, follow=True):
            text = progress.feed(decoder.decode(chunk))
            if text and not post(log, text):
                return
        tail = progress.feed(decoder.decode(b"", final=True)) + progress.flush()
        if tail:
            post(log, tail)
        code = int((helper.wait() or {}).get("StatusCode", -1))
        post(settle, done.set_result, code)
    except Exception as exc:  # noqa: BLE001 - reported through the job
        post(settle, done.set_exception, exc)


async def follow_self_update(ctx: JobContext, helper, timeout: float = 600, grace: float = 30) -> None:
    """Follow the helper until it exits (or this dashboard is replaced and killed).

    The log is followed in a daemon thread, not asyncio.to_thread: when the helper
    recreates this container the thread never ends, and the default executor is
    joined at shutdown.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future[int] = loop.create_future()
    done.add_done_callback(lambda f: f.cancelled() or f.exception())  # retrieved even after a timeout
    threading.Thread(
        target=_watch_helper, args=(helper, loop, ctx.log, done), daemon=True,
        name="self-update-helper-log",
    ).start()
    try:
        code = await asyncio.wait_for(asyncio.shield(done), timeout)
    except asyncio.TimeoutError:
        raise UpdateFailed(
            "The dashboard was not restarted in time – check the helper container on the server"
        ) from None
    try:
        if code != 0:
            raise UpdateFailed(f"The self-update helper failed (exit code {code}) – see the log above")
        # Exit 0 and still alive: nothing changed, so compose didn't recreate us.
        await asyncio.sleep(grace)
        ctx.log(NOTHING_TO_RESTART + "\n")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(helper.remove, force=True)
