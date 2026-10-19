import httpx
import pytest

from app.jobs import JobContext
from app.mailcow import check_mailcow, host_cmd, run_capture, update_mailcow
from app.updaters import UpdateFailed

RELEASE_URL = "https://api.github.com/repos/mailcow/mailcow-dockerized/releases/latest"
DIR = "/opt/mailcow-dockerized"


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


def test_host_cmd_exact_and_quoting():
    assert host_cmd(DIR, "./update.sh --check") == [
        "nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-C", "--",
        "env", "-i", "HOME=/root", "USER=root", "LOGNAME=root",
        "bash", "-lc", "cd /opt/mailcow-dockerized && ./update.sh --check",
    ]
    assert host_cmd("/opt/my mail", "ls")[-1] == "cd '/opt/my mail' && ls"


def fake_run(check_rc, check_out="", describe=(0, "2024-01a\n")):
    calls = []

    async def run(cmd):
        calls.append(cmd)
        if cmd == host_cmd(DIR, "./update.sh --check"):
            return check_rc, check_out
        return describe

    return run, calls


@pytest.mark.parametrize("rc,expected", [(0, True), (3, False)])
async def test_check_exit_codes(respx_mock, http, rc, expected):
    respx_mock.get(RELEASE_URL).mock(return_value=httpx.Response(200, json={"tag_name": "2024-02"}))
    run, calls = fake_run(rc)
    st = await check_mailcow(DIR, run, http)
    assert st.update_available is expected
    assert st.error is None
    assert st.current_version == "2024-01a"
    assert st.latest_release == "2024-02"
    assert calls == [
        host_cmd(DIR, "./update.sh --check"),
        host_cmd(DIR, "git describe --tags --always"),
    ]
    assert respx_mock.calls.last.request.headers["Accept"] == "application/vnd.github+json"


async def test_check_error_includes_code_and_last_lines(respx_mock, http):
    respx_mock.get(RELEASE_URL).mock(return_value=httpx.Response(200, json={"tag_name": "x"}))
    out = "\n".join(f"line{i}" for i in range(1, 9)) + "\n\n"
    run, _ = fake_run(1, out)
    st = await check_mailcow(DIR, run, http)
    assert st.update_available is None
    assert "1" in st.error
    assert "line8" in st.error and "line4" in st.error
    assert "line3" not in st.error


async def test_check_explains_missing_host_access(respx_mock, http):
    respx_mock.get(RELEASE_URL).mock(return_value=httpx.Response(200, json={"tag_name": "x"}))
    run, _ = fake_run(1, "nsenter: reassociate to namespaces failed: Operation not permitted\n",
                      describe=(1, "nsenter: reassociate to namespaces failed\n"))
    st = await check_mailcow(DIR, run, http)
    assert "pid: host" in st.error and "privileged: true" in st.error
    assert "nsenter" not in st.error and st.current_version is None


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, json={}), httpx.ConnectError("boom")],
)
async def test_github_failure_is_not_an_error(respx_mock, http, response):
    mock = respx_mock.get(RELEASE_URL)
    if isinstance(response, Exception):
        mock.mock(side_effect=response)
    else:
        mock.mock(return_value=response)
    run, _ = fake_run(0)
    st = await check_mailcow(DIR, run, http)
    assert st.latest_release is None
    assert st.error is None
    assert st.update_available is True


async def test_git_describe_failure_is_not_an_error(respx_mock, http):
    respx_mock.get(RELEASE_URL).mock(return_value=httpx.Response(200, json={"tag_name": "t"}))
    run, _ = fake_run(3, describe=(128, "fatal: not a git repository"))
    st = await check_mailcow(DIR, run, http)
    assert st.current_version is None
    assert st.error is None
    assert st.update_available is False


def make_ctx(rcs, output=""):
    ctx = JobContext(1, lambda text: logs.append(text))
    logs: list[str] = []
    calls = []
    seq = list(rcs)

    async def run(cmd, cwd=None, timeout=1800, stdin_devnull=True):
        calls.append((cmd, timeout))
        ctx.last_output = output
        return seq.pop(0)

    ctx.run = run
    return ctx, calls, logs


async def test_update_reruns_once_on_exit_2():
    ctx, calls, logs = make_ctx([2, 0])
    await update_mailcow(ctx, DIR)
    assert [c[0] for c in calls] == [host_cmd(DIR, "./update.sh --force")] * 2
    assert all(c[1] == 3600 for c in calls)
    assert any("update.sh updated itself – running it again" in t for t in logs)


async def test_update_stops_after_second_exit_2():
    ctx, calls, _ = make_ctx([2, 2])
    with pytest.raises(UpdateFailed, match="Mailcow update stopped – see log"):
        await update_mailcow(ctx, DIR)
    assert len(calls) == 2


async def test_update_success_single_run():
    ctx, calls, _ = make_ctx([0])
    await update_mailcow(ctx, DIR)
    assert len(calls) == 1


@pytest.mark.parametrize("output", ["CONFLICT (content): merge", "You have Local Changes"])
async def test_update_failure_mentions_local_changes(output):
    ctx, _, _ = make_ctx([1], output)
    with pytest.raises(UpdateFailed, match="local changes"):
        await update_mailcow(ctx, DIR)


async def test_update_failure_without_hint():
    ctx, _, _ = make_ctx([1], "boom")
    with pytest.raises(UpdateFailed) as ei:
        await update_mailcow(ctx, DIR)
    assert "local changes" not in str(ei.value)


async def test_update_explains_missing_host_access():
    ctx, _, _ = make_ctx([1], "nsenter: reassociate to namespaces failed: Operation not permitted")
    with pytest.raises(UpdateFailed, match="privileged: true"):
        await update_mailcow(ctx, DIR)


async def test_run_capture_output_and_exit_code():
    rc, out = await run_capture(["/bin/sh", "-c", "echo hi; echo err >&2; exit 3"])
    assert rc == 3
    assert "hi" in out and "err" in out


async def test_run_capture_timeout_kills_group():
    rc, out = await run_capture(["/bin/sh", "-c", "echo start; sleep 30 & sleep 30"], timeout=0.5)
    assert rc == -1
    assert "Timed out after 0.5 s" in out


async def test_run_capture_does_not_leak_dashboard_env(monkeypatch):
    monkeypatch.setenv("ACS_CONNECTION_STRING", "endpoint=x;accesskey=secret")
    monkeypatch.setenv("TZ", "Europe/London")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    rc, out = await run_capture(["/bin/sh", "-c", "env"])
    assert rc == 0
    names = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
    assert "ACS_CONNECTION_STRING" not in names and "TZ" not in names
    assert "PATH" in names and "DOCKER_HOST" in names
