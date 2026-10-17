"""Mailcow check and update, run on the host exactly as `./update.sh` would be by hand."""

import asyncio
import contextlib
import os
import shlex
import signal
from collections.abc import Awaitable, Callable

import httpx

from app.checker import MailcowStatus
from app.jobs import JobContext, subprocess_env
from app.updaters import UpdateFailed

RELEASE_URL = "https://api.github.com/repos/mailcow/mailcow-dockerized/releases/latest"
_HINT = (
    " – the Mailcow git tree may have local changes or merge conflicts;"
    " resolve them by hand on the server"
)


def host_cmd(mailcow_dir: str, script: str) -> list[str]:
    return [
        "nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-C", "--",
        # A clean environment, but with what a root login has (update.sh and git use HOME).
        "env", "-i", "HOME=/root", "USER=root", "LOGNAME=root",
        "bash", "-lc", f"cd {shlex.quote(mailcow_dir)} && {script}",
    ]


async def run_capture(cmd: list[str], timeout: float = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=subprocess_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    chunks: list[bytes] = []

    async def pump() -> int:
        async for chunk in proc.stdout:
            chunks.append(chunk)
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(pump(), timeout)
        return rc, b"".join(chunks).decode(errors="replace")
    except asyncio.TimeoutError:
        out = b"".join(chunks).decode(errors="replace")
        return -1, out + f"Timed out after {timeout:g} s"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        if proc.returncode is None:
            await proc.wait()


async def _latest_release(http: httpx.AsyncClient) -> str | None:
    try:
        resp = await http.get(
            RELEASE_URL, headers={"Accept": "application/vnd.github+json"}, timeout=10
        )
        if resp.status_code != 200:
            return None
        tag = resp.json().get("tag_name")
        return tag if isinstance(tag, str) and tag else None
    except Exception:  # noqa: BLE001 - display-only, never an error
        return None


async def check_mailcow(
    mailcow_dir: str,
    run: Callable[[list[str]], Awaitable[tuple[int, str]]],
    http: httpx.AsyncClient,
) -> MailcowStatus:
    rc, output = await run(host_cmd(mailcow_dir, "./update.sh --check"))
    available: bool | None = None
    error: str | None = None
    if rc == 0:
        available = True
    elif rc == 3:
        available = False
    else:
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()][-5:]
        error = f"Mailcow check failed (exit code {rc})"
        if lines:
            error += ": " + " | ".join(lines)

    current: str | None = None
    drc, dout = await run(host_cmd(mailcow_dir, "git describe --tags --always"))
    if drc == 0 and dout.strip():
        current = dout.strip().splitlines()[-1].strip()

    return MailcowStatus(
        update_available=available,
        current_version=current,
        latest_release=await _latest_release(http),
        error=error,
    )


async def update_mailcow(ctx: JobContext, mailcow_dir: str) -> None:
    cmd = host_cmd(mailcow_dir, "./update.sh --force")
    rc = await ctx.run(cmd, timeout=3600)
    if rc == 2:
        ctx.log("update.sh updated itself – running it again\n")
        rc = await ctx.run(cmd, timeout=3600)
    if rc != 0:
        low = ctx.last_output.lower()
        hint = _HINT if "conflict" in low or "local changes" in low else ""
        raise UpdateFailed(f"Mailcow update stopped – see log{hint}")
