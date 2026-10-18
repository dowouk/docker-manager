import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable

from app.logfilter import is_pull_progress
from app.store import Store

BUSY_MESSAGE = "Another update is running – try again when it finishes"
_END = "__END__:"
INTERRUPTED_MESSAGE = "Interrupted – the dashboard is stopping"
_DRAIN_GRACE = 1.0


_ENV_KEYS = ("PATH", "HOME", "LANG")
_ENV_PREFIXES = ("LC_", "DOCKER_", "COMPOSE_")


_docker_config_dir: str | None = None


def set_docker_config_dir(path: str | None) -> None:
    """The generated Docker config (credential-helper entries) to use for children."""
    global _docker_config_dir
    _docker_config_dir = path


def subprocess_env() -> dict[str, str]:
    """Environment for child processes: only what docker/compose need.

    The dashboard's own settings (secrets, TZ, DATA_DIR, …) must not reach
    `docker compose`, which lets shell variables override a project's `.env`.

    The one exception is REGISTRY_AUTH_FILE: the Docker CLI sends registry logins
    with each pull, reading them from DOCKER_CONFIG/config.json. When the file is a
    mounted Docker config.json, point DOCKER_CONFIG at its folder, so the logins
    that let the dashboard check a private image also let it pull that image.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_KEYS or key.startswith(_ENV_PREFIXES)
    }
    if _docker_config_dir:
        # The generated config names the credential helper and wins over everything:
        # it also covers the hosts in the 0.1 auth file.
        env["DOCKER_CONFIG"] = _docker_config_dir
        return env
    auth_file = os.environ.get("REGISTRY_AUTH_FILE")
    if auth_file and "DOCKER_CONFIG" not in env and os.path.basename(auth_file) == "config.json":
        env["DOCKER_CONFIG"] = os.path.dirname(auth_file) or "."
    return env


class BusyError(Exception):
    def __init__(self, message: str = BUSY_MESSAGE) -> None:
        super().__init__(message)


class JobContext:
    def __init__(self, job_id: int, log: Callable[[str], None]) -> None:
        self.job_id = job_id
        self.log = log
        self.last_output = ""

    async def run(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: float = 1800,
        stdin_devnull: bool = True,
    ) -> int:
        self.last_output = ""
        self.log("$ " + " ".join(cmd) + "\n")
        env = subprocess_env()
        if cwd is not None:
            # As a shell would: compose files use ${PWD} for the project folder.
            env["PWD"] = cwd
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL if stdin_devnull else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1 << 20,
            start_new_session=True,  # own process group so we can kill descendants
        )

        async def pump() -> None:
            async for line in proc.stdout:
                text = line.decode(errors="replace")
                self.last_output += text
                if not is_pull_progress(text):
                    self.log(text)

        reader = asyncio.ensure_future(pump())

        async def body() -> int:
            # proc.wait() also waits for the pipes to close, which a lingering
            # grandchild can prevent; poll the exit status instead.
            while proc.returncode is None:
                await asyncio.sleep(0.05)
            rc = proc.returncode
            # Child has exited; drain remaining output but don't let a lingering
            # grandchild holding the pipe open block us.
            try:
                await asyncio.wait_for(asyncio.shield(reader), _DRAIN_GRACE)
            except asyncio.TimeoutError:
                pass
            return rc

        try:
            return await asyncio.wait_for(body(), timeout)
        except asyncio.TimeoutError:
            self.log(f"Timed out after {timeout:g} s\n")
            return -1
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            await proc.wait()


class JobRunner:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._task: asyncio.Task | None = None
        self._subscribers: dict[int, list[asyncio.Queue[str]]] = {}

    @property
    def busy(self) -> bool:
        return self._task is not None

    def submit(
        self,
        kind: str,
        target: str,
        user: str,
        work: Callable[[JobContext], Awaitable[None]],
    ) -> int:
        if self._task is not None:
            raise BusyError()
        job_id = self._store.create_job(kind, target, user)
        self._subscribers[job_id] = []
        self._task = asyncio.get_running_loop().create_task(self._run(job_id, work))
        return job_id

    def _log(self, job_id: int, text: str) -> None:
        # Synchronous (no awaits) so store write and fan-out are atomic with
        # respect to subscribe()'s replay-then-register step.
        self._store.append_log(job_id, text)
        for q in self._subscribers.get(job_id, []):
            q.put_nowait(text)

    async def _run(self, job_id: int, work: Callable[[JobContext], Awaitable[None]]) -> None:
        ctx = JobContext(job_id, lambda text: self._log(job_id, text))
        status = "succeeded"
        try:
            try:
                await work(ctx)
            except Exception as exc:  # noqa: BLE001 - job failure is reported, not raised
                status = "failed"
                self._log(job_id, f"Error: {exc}\n")
            except asyncio.CancelledError:
                # The dashboard is shutting down (e.g. replaced by a self-update). Leave
                # the row running: the next startup classifies it (mark_interrupted_jobs).
                status = "interrupted"
                self._log(job_id, INTERRUPTED_MESSAGE + "\n")
                raise
            finally:
                if status != "interrupted":
                    self._store.finish_job(job_id, status)
        finally:
            # Always release the runner and tell subscribers, even if the store fails.
            self._task = None
            for q in self._subscribers.pop(job_id, []):
                q.put_nowait(_END + status)

    async def subscribe(self, job_id: int) -> AsyncIterator[str]:
        job = self._store.get_job(job_id)
        if job is None:
            yield _END + "unknown"
            return
        queue: asyncio.Queue[str] | None = None
        subs = self._subscribers.get(job_id)
        if subs is not None:  # running: register in the same step as the snapshot
            queue = asyncio.Queue()
            subs.append(queue)
        try:
            if job.log:
                yield job.log
            if queue is None:
                yield _END + job.status
                return
            while True:
                item = await queue.get()
                yield item
                if item.startswith(_END):
                    return
        finally:
            if queue is not None and subs is not None and queue in subs:
                subs.remove(queue)
