import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timezone

import docker.errors
import httpx
import pytest

from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status
from app.imageref import parse_image_ref
from app.inventory import ContainerInfo
from app.jobs import BUSY_MESSAGE, BusyError
from app.config import Settings
from app.main import apply_settings, create_app
from app.store import Store
from notify_fakes import FakeProvider, providers_for
from auth_helpers import CHECKING_FORM, CSRF, assert_all_routes_guarded, logged_in_client, sign_in

CHECKED_AT = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)  # 08:00 BST


class FakeRunner:
    def __init__(self, busy=False):
        self.busy = busy
        self.submitted = []

    def submit(self, kind, target, user, work):
        if self.busy:
            raise BusyError()
        self.submitted.append((kind, target, user, work))
        return 7

    async def subscribe(self, job_id, idle=None):
        yield "line one\nline two\n"
        yield "line three\n"
        yield "__END__:succeeded"


class StubContainer:
    def __init__(self, cid, name, image_ref, image_id, state="running", labels=None):
        self.id = cid
        self.name = name
        self.labels = labels or {}
        self.status = state
        self.attrs = {
            "Config": {"Image": image_ref, "Labels": self.labels},
            "Image": image_id,
            "State": {"Status": state},
        }


class StubImages:
    def get(self, ref):
        raise docker.errors.ImageNotFound(f"no such image: {ref}")


class StubContainers:
    def __init__(self, items):
        self.items = items

    def list(self, all=False):
        return self.items


class StubDocker:
    def __init__(self, containers=()):
        self.containers = StubContainers(list(containers))
        self.images = StubImages()


def info(tmp_path, name, image="nginx:latest", **kw):
    values = dict(
        id=name + "0" * (64 - len(name)), name=name, image_ref=image, image_id="sha256:img",
        running_repo_digests=["nginx@sha256:0123456789abcdef0123"],
        local_tag_image_id="sha256:img", project="site", working_dir=str(tmp_path),
        config_files=[], service=name, state="running", is_self=False,
    )
    values.update(kw)
    return ContainerInfo(**values)


def status(c, st=Status.UP_TO_DATE, **kw):
    return ContainerStatus(container=c, status=st, **kw)


@pytest.fixture
def compose_file(tmp_path):
    path = tmp_path / "compose.yml"
    path.write_text("services:\n  db:\n    image: postgres:16.2\n")
    return str(path)


@pytest.fixture
def seeded(tmp_path, compose_file):
    web = info(tmp_path, "web")
    db = info(tmp_path, "db", image="postgres:16.2", config_files=[compose_file])
    lone = info(tmp_path, "lone", project=None, working_dir=None, service=None)
    me = info(tmp_path, "dash", image="ghcr.io/x/docker-manager:latest",
              project="dash", is_self=True)
    result = CheckResult(
        checked_at=CHECKED_AT,
        containers=[
            status(web, Status.NEWER_BUILD),
            status(db, minor_update="16.3", major_update="17.0"),
            status(lone, Status.NEWER_BUILD, updatable=False,
                   not_updatable_reason="Not started by compose"),
            status(me, Status.NEWER_BUILD),
        ],
        mailcow=MailcowStatus(update_available=True, current_version="2026-09",
                              latest_release="2026-10"),
    )
    return result


def make_app(settings, tmp_path, result=None, runner=None, docker_client=None):
    store = Store(str(tmp_path / "data" / "dashboard.db"))
    if result is not None:
        store.save_check(result.to_dict(), result.checked_at)
    app = create_app(
        settings,
        docker_client=docker_client or StubDocker(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        store=store,
        runner=runner or FakeRunner(),
        start_scheduler=False,
    )
    return app


@pytest.fixture
def client_for(settings, tmp_path):
    clients = []

    def factory(**kw):
        app = make_app(settings, tmp_path, **kw)
        c = logged_in_client(app, follow_redirects=False)
        c.__enter__()
        clients.append(c)
        return c, app

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


def test_all_routes_guarded(settings, tmp_path, seeded):
    assert_all_routes_guarded(make_app(settings, tmp_path, seeded))


def _table(html: str) -> str:
    """Just the "All containers" table – update buttons also appear on the attention cards."""
    return html[html.index('<table class="data containers">'):html.index("</table>")]


def test_dashboard_rows_by_urgency_with_badges(client_for, seeded):
    c, _ = client_for(result=seeded)
    r = c.get("/")
    assert r.status_code == 200
    html = r.text
    assert "test-server" in html
    assert "17 Oct 2026, 08:00" in html
    assert "Test User" in html
    # one flat table: Mailcow first, then updates by name; the stack is shown on each row
    names = re.findall(r'<div class="who-name">([^<]+?)\s*(?:<|$)', _table(html))
    assert names == ["Mailcow", "dash", "db", "lone", "web"]
    assert "stack · site" in html and "no stack" in html
    # stat cards and attention cards agree with the rows
    assert "4 need your attention" not in html and "5 need your attention" in html
    assert html.count('class="card attention-card"') == 5
    assert "Newer build available" in html
    assert "Up to date" in html
    assert "Major version – may need a data migration. Update by hand." in html
    assert "0123456789ab" in html  # 12-char short digest
    assert "0123456789abc" not in html
    assert "Mailcow" in html and "2026-10" in html
    assert 'action="/mailcow/update"' in html
    assert f'value="{CSRF}"' in html
    assert 'action="/auth/logout"' in html
    db_id = seeded.containers[1].container.id
    assert f"/containers/{db_id}/version-preview?tag=16.3" in html


def test_not_compose_row_has_no_update_button(client_for, seeded):
    c, _ = client_for(result=seeded)
    html = c.get("/").text
    lone_id = seeded.containers[2].container.id
    web_id = seeded.containers[0].container.id
    assert f"/containers/{web_id}/update" in html
    assert lone_id in html or "lone" in html
    assert f"/containers/{lone_id}/update" not in html  # the skip form is fine
    assert "Not started by compose" in html


def test_self_row_posts_to_self_update(client_for, seeded):
    c, _ = client_for(result=seeded)
    html = c.get("/").text
    me_id = seeded.containers[3].container.id
    assert 'action="/self/update"' in html
    assert f"/containers/{me_id}/update" not in html


def test_version_preview_rejects_other_tags(client_for, seeded):
    c, _ = client_for(result=seeded)
    db_id = seeded.containers[1].container.id
    assert c.get(f"/containers/{db_id}/version-preview?tag=17.0").status_code == 400
    assert c.get(f"/containers/{db_id}/version-preview?tag=latest").status_code == 400
    assert c.get("/containers/nope/version-preview?tag=16.3").status_code == 404


def test_version_preview_shows_diff(client_for, seeded):
    c, _ = client_for(result=seeded)
    db_id = seeded.containers[1].container.id
    r = c.get(f"/containers/{db_id}/version-preview?tag=16.3")
    assert r.status_code == 200
    assert "-    image: postgres:16.2" in r.text
    assert "+    image: postgres:16.3" in r.text
    assert f'action="/containers/{db_id}/version"' in r.text
    assert "<html" not in r.text  # fragment


def test_version_preview_shows_edit_refused(client_for, seeded, compose_file):
    with open(compose_file, "w") as f:
        f.write("services:\n  db:\n    image: ${DB_IMAGE}\n")
    c, _ = client_for(result=seeded)
    db_id = seeded.containers[1].container.id
    r = c.get(f"/containers/{db_id}/version-preview?tag=16.3")
    assert r.status_code == 200
    assert "update by hand" in r.text
    assert "/version\"" not in r.text


def preview_sha(c, cid):
    html = c.get(f"/containers/{cid}/version-preview?tag=16.3").text
    return re.search(r'name="sha" value="([0-9a-f]{64})"', html).group(1)


def test_version_submit(client_for, seeded):
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    db_id = seeded.containers[1].container.id
    sha = preview_sha(c, db_id)
    r = c.post(f"/containers/{db_id}/version", data={"tag": "17.0", "sha": sha, "csrf_token": CSRF})
    assert r.status_code == 400
    r = c.post(f"/containers/{db_id}/version", data={"tag": "16.3", "sha": sha, "csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs/7"
    kind, target, user, _ = runner.submitted[0]
    assert (kind, user) == ("version", "test@example.com")
    assert "db" in target and "16.3" in target


def test_version_job_uses_health_timeout_setting(client_for, seeded, settings, monkeypatch):
    seen = {}

    async def fake_update_version(ctx, info, change, backup_dir, wait, **kw):
        seen.update(kw)

    monkeypatch.setattr("app.main.update_version", fake_update_version)
    monkeypatch.setattr("app.main.run_check", lambda app: asyncio.sleep(0))
    settings.health_timeout_seconds = 42
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    db_id = seeded.containers[1].container.id
    sha = preview_sha(c, db_id)
    c.post(f"/containers/{db_id}/version", data={"tag": "16.3", "sha": sha, "csrf_token": CSRF})
    work = runner.submitted[0][3]
    ctx = type("Ctx", (), {"log": staticmethod(lambda t: None)})()
    asyncio.run(work(ctx))
    assert seen["health_timeout"] == 42
    assert str(seen["tz"]) == settings.tz  # M10: backups are stamped in local time


def test_update_submits_job(client_for, seeded):
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    web_id = seeded.containers[0].container.id
    r = c.post(f"/containers/{web_id}/update", data={"mode": "build", "csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs/7"
    assert runner.submitted[0][0] == "build"
    r = c.post(f"/containers/{web_id}/update", data={"mode": "bogus", "csrf_token": CSRF})
    assert r.status_code == 400


def test_update_refused_for_unknown_or_not_updatable(client_for, seeded):
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    lone_id = seeded.containers[2].container.id
    r = c.post("/containers/nope/update", data={"mode": "build", "csrf_token": CSRF})
    assert r.status_code == 404
    r = c.post(f"/containers/{lone_id}/update", data={"mode": "build", "csrf_token": CSRF})
    assert r.status_code == 400
    assert runner.submitted == []


def test_post_requires_csrf(client_for, seeded):
    c, _ = client_for(result=seeded)
    web_id = seeded.containers[0].container.id
    r = c.post(f"/containers/{web_id}/update", data={"mode": "build"})
    assert r.status_code == 403


def test_update_while_busy_redirects_with_message(client_for, seeded):
    c, _ = client_for(result=seeded, runner=FakeRunner(busy=True))
    web_id = seeded.containers[0].container.id
    r = c.post(f"/containers/{web_id}/update", data={"mode": "build", "csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/?msg=busy"
    assert BUSY_MESSAGE in c.get("/?msg=busy").text


def test_unknown_flash_code_not_reflected(client_for, seeded):
    c, _ = client_for(result=seeded)
    r = c.get("/?msg=<b>evil</b>")
    assert r.status_code == 200
    assert "evil" not in r.text


def test_mailcow_update_submits_job(client_for, seeded):
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    r = c.post("/mailcow/update", data={"csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs/7"
    assert runner.submitted[0][0] == "mailcow"


class ExitedHelper:
    def logs(self, stream=False, follow=False):
        yield b"Pulling dash\n"

    def wait(self):
        return {"StatusCode": 0}

    def remove(self, force=False):
        pass


def test_self_update_runs_as_job(client_for, seeded, monkeypatch):
    calls = []

    def start(*a):
        calls.append(a)
        return ExitedHelper()

    monkeypatch.setattr("app.main.start_self_update", start)
    monkeypatch.setattr("app.main.SELF_UPDATE_GRACE", 0)
    monkeypatch.setattr("app.main.run_check", lambda app: asyncio.sleep(0))
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    r = c.post("/self/update", data={"csrf_token": CSRF})
    assert r.status_code == 200
    assert "refresh by itself" in r.text
    # The page waits for the restart (polling /healthz) and then opens the job's log.
    assert 'data-await-restart' in r.text and 'data-job="7"' in r.text
    assert 'href="/jobs/7"' in r.text
    kind, target, user, work = runner.submitted[0]
    assert (kind, target, user) == ("self", "dash", "test@example.com")
    assert calls == []  # nothing happens until the job runs

    logged = []
    ctx = type("Ctx", (), {"log": staticmethod(logged.append)})()
    asyncio.run(work(ctx))  # helper exited 0 and the dashboard is still here
    assert calls and calls[0][1].is_self
    text = "".join(logged)
    assert "Helper started" in text and "Pulling dash\n" in text
    assert "Already up to date – nothing to restart" in text


def test_self_update_while_busy(client_for, seeded, monkeypatch):
    monkeypatch.setattr("app.main.start_self_update", lambda *a: pytest.fail("must not run"))
    c, _ = client_for(result=seeded, runner=FakeRunner(busy=True))
    r = c.post("/self/update", data={"csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/?msg=busy"


def test_self_update_failure_is_logged(client_for, seeded, monkeypatch, caplog):
    def boom(*a):
        raise docker.errors.DockerException("socket gone")

    monkeypatch.setattr("app.main.start_self_update", boom)
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    c.post("/self/update", data={"csrf_token": CSRF})
    work = runner.submitted[0][3]
    ctx = type("Ctx", (), {"log": staticmethod(lambda t: None)})()
    with caplog.at_level(logging.ERROR, logger="app.main"):
        with pytest.raises(docker.errors.DockerException):
            asyncio.run(work(ctx))
    assert "Could not start the dashboard self-update" in caplog.text
    assert "socket gone" in caplog.text


def test_history_lists_jobs(client_for, seeded):
    c, app = client_for(result=seeded)
    job_id = app.state.store.create_job("build", "web", "someone@example.com")
    app.state.store.finish_job(job_id, "succeeded")
    app.state.store.create_job("self", "dash", "someone@example.com")
    html = c.get("/history").text
    assert "Image update – web" in html
    assert "Dashboard self-update – dash" in html
    assert "someone@example.com" in html
    assert f'href="/jobs/{job_id}"' in html
    assert "Succeeded" in html


def test_job_page_and_stream(client_for, seeded):
    c, app = client_for(result=seeded)
    job_id = app.state.store.create_job("build", "web", "someone@example.com")
    r = c.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert f'data-job-id="{job_id}"' in r.text
    assert c.get("/jobs/999").status_code == 404
    r = c.get(f"/jobs/{job_id}/stream")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    assert r.text == (
        "data: line one\ndata: line two\n\n"
        "data: line three\n\n"
        "event: end\ndata: succeeded\n\n"
    )


def test_dashboard_runs_check_when_none_stored(client_for, tmp_path):
    # Review Focus 4: an exited, non-compose container whose image was removed.
    gone = StubContainer("f" * 64, "orphan", "removed/image:1.0", "sha256:gone", state="exited")
    c, app = client_for(docker_client=StubDocker([gone]))
    r = c.get("/")
    assert r.status_code == 200
    assert "orphan" in r.text
    assert "Can&#39;t check" in r.text
    assert app.state.store.latest_check() is not None


def test_check_now(client_for, seeded):
    c, app = client_for(result=seeded)
    r = c.post("/check", data={"csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    _, payload = app.state.store.latest_check()
    assert payload["containers"] == []


async def test_job_triggers_check_afterwards(settings, tmp_path, seeded, monkeypatch):
    ran = []

    async def fake_update_mailcow(ctx, mailcow_dir):
        ran.append(mailcow_dir)
        raise RuntimeError("boom")

    monkeypatch.setattr("app.main.update_mailcow", fake_update_mailcow)
    runner = FakeRunner()
    app = make_app(settings, tmp_path, seeded, runner=runner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        sign_in(c, app)
        r = await c.post("/mailcow/update", data={"csrf_token": CSRF})
    assert r.status_code == 303
    work = runner.submitted[0][3]
    with pytest.raises(RuntimeError):
        await work(object())
    assert ran == [settings.mailcow_dir]
    tasks = list(app.state.background)
    assert len(tasks) == 1
    await tasks[0]
    _, payload = app.state.store.latest_check()
    assert payload["containers"] == []  # the fresh check replaced the seeded one


def test_version_refused_without_service_label(client_for, tmp_path, compose_file):
    db = info(tmp_path, "db", image="postgres:16.2", config_files=[compose_file], service=None)
    result = CheckResult(CHECKED_AT, [status(db, minor_update="16.3")])
    c, _ = client_for(result=result)
    assert c.get(f"/containers/{db.id}/version-preview?tag=16.3").status_code == 400
    r = c.post(f"/containers/{db.id}/version", data={"tag": "16.3", "sha": "x", "csrf_token": CSRF})
    assert r.status_code == 400


class BrokenDocker(StubDocker):
    def __init__(self, message=""):
        super().__init__()

        def fail(all=False):
            raise docker.errors.DockerException(message)

        self.containers.list = fail


def test_version_refused_if_file_changed_since_preview(client_for, seeded, compose_file):
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    db_id = seeded.containers[1].container.id
    sha = preview_sha(c, db_id)
    with open(compose_file, "a") as f:
        f.write("  # edited after the preview\n")
    r = c.post(f"/containers/{db_id}/version", data={"tag": "16.3", "sha": sha, "csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/?msg=changed"
    assert runner.submitted == []
    assert "The compose file changed since the preview" in c.get("/?msg=changed").text


def test_version_post_edit_refused_redirects(client_for, seeded, compose_file, caplog):
    with open(compose_file, "w") as f:
        f.write("services:\n  db:\n    image: ${DB_IMAGE}\n")
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    db_id = seeded.containers[1].container.id
    with caplog.at_level(logging.ERROR, logger="app.main"):
        r = c.post(f"/containers/{db_id}/version",
                   data={"tag": "16.3", "sha": "x", "csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=edit_refused"
    assert runner.submitted == []
    assert "update by hand" in c.get("/?msg=edit_refused").text
    assert "refused" in caplog.text


def test_check_now_failure_logged_and_flashed(client_for, seeded, caplog):
    c, _ = client_for(result=seeded, docker_client=BrokenDocker("daemon down"))
    with caplog.at_level(logging.ERROR, logger="app.main"):
        r = c.post("/check", data={"csrf_token": CSRF})
    assert r.status_code == 303
    assert r.headers["location"] == "/?msg=check_failed"
    assert "Check now failed" in caplog.text and "daemon down" in caplog.text
    assert "The check could not be completed" in c.get("/?msg=check_failed").text


def test_dashboard_check_failure_logged_with_type_name(client_for, caplog):
    c, _ = client_for(docker_client=BrokenDocker(""))
    with caplog.at_level(logging.ERROR, logger="app.main"):
        r = c.get("/")
    assert r.status_code == 200
    assert "The check could not be completed: DockerException" in r.text
    assert "Check failed while loading the dashboard" in caplog.text


def test_background_check_failure_is_logged(settings, tmp_path, seeded, caplog, monkeypatch):
    async def ok(ctx, mailcow_dir):
        return None

    monkeypatch.setattr("app.main.update_mailcow", ok)
    runner = FakeRunner()
    app = make_app(settings, tmp_path, seeded, runner=runner,
                   docker_client=BrokenDocker("daemon down"))
    with logged_in_client(app, follow_redirects=False) as c:
        c.post("/mailcow/update", data={"csrf_token": CSRF})
    work = runner.submitted[0][3]

    async def go():
        await work(object())
        await asyncio.gather(*app.state.background, return_exceptions=True)
        await asyncio.sleep(0)  # let done-callbacks run

    with caplog.at_level(logging.ERROR, logger="app.main"):
        asyncio.run(go())
    assert "Background check after a job failed" in caplog.text
    assert "daemon down" in caplog.text


def digest_app(settings, tmp_path, provider, result=None, **overrides):
    s = settings.model_copy(update={
        "acs_connection_string": "endpoint=https://x;accesskey=k",
        "digest_from": "dash@example.com",
        "digest_to": ["ops@example.com"],
        **overrides,
    })
    store = Store(str(tmp_path / "data" / "dashboard.db"))
    if result is not None:
        store.save_check(result.to_dict(), result.checked_at)
    app = create_app(
        s, docker_client=StubDocker(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        store=store, runner=FakeRunner(), start_scheduler=False,
        providers=providers_for(provider),
    )
    c = logged_in_client(app, follow_redirects=False)
    c.__enter__()
    return c


def test_digest_send_sends_latest_check(settings, tmp_path, seeded):
    fake = FakeProvider()
    c = digest_app(settings, tmp_path, fake, seeded)
    r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.status_code == 303 and r.headers["location"] == "/?msg=digest_sent"
    assert fake.sent[0][1].subject == "test-server: 5 updates available"
    assert fake.sent[0][1].recipients == ("ops@example.com",)
    assert "Digest sent" in c.get("/?msg=digest_sent").text
    c.__exit__(None, None, None)


def test_digest_send_runs_check_when_none_stored(settings, tmp_path):
    fake = FakeProvider()
    c = digest_app(settings, tmp_path, fake)
    r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_sent"
    assert fake.sent[0][1].subject == "test-server: all up to date"
    c.__exit__(None, None, None)


def test_digest_send_nothing_to_report(settings, tmp_path):
    fake = FakeProvider()
    c = digest_app(settings, tmp_path, fake, digest_when_none=False)
    r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_nothing"
    assert fake.sent == []
    assert "nothing to report" in c.get("/?msg=digest_nothing").text
    c.__exit__(None, None, None)


def test_digest_send_not_configured(client_for, seeded):
    c, _ = client_for(result=seeded)
    r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_unconfigured"
    assert "no channel gets the digest" in c.get("/?msg=digest_unconfigured").text


def test_digest_send_failure_logged(settings, tmp_path, seeded, caplog):
    c = digest_app(settings, tmp_path, FakeProvider(fail=RuntimeError("acs down accesskey=TOPSECRET")),
                   seeded)
    with caplog.at_level(logging.WARNING):
        r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_failed"
    assert "Notification to Email (from environment) failed (RuntimeError)" in caplog.text
    assert "acs down" not in caplog.text and "TOPSECRET" not in caplog.text
    assert "could not be sent" in c.get("/?msg=digest_failed").text
    c.__exit__(None, None, None)


def test_digest_send_requires_csrf(settings, tmp_path, seeded):
    fake = FakeProvider()
    c = digest_app(settings, tmp_path, fake, seeded)
    assert c.post("/digest/send", data={"csrf_token": "wrong"}).status_code == 403
    assert fake.sent == []
    c.__exit__(None, None, None)


def test_digest_button_only_when_enabled(client_for, settings, tmp_path, seeded):
    c, _ = client_for(result=seeded)
    assert "/digest/send" not in c.get("/").text
    c2 = digest_app(settings, tmp_path, FakeProvider(), seeded)
    assert 'action="/digest/send"' in c2.get("/").text
    c2.__exit__(None, None, None)


def test_stopped_container_note_next_to_update(client_for, tmp_path):
    web = info(tmp_path, "web", state="exited")
    result = CheckResult(CHECKED_AT, [status(web, Status.NEWER_BUILD)])
    c, _ = client_for(result=result)
    html = c.get("/").text
    assert f"/containers/{web.id}/update" in html
    assert "Stopped – updating will start it" in html
    running = CheckResult(CHECKED_AT, [status(info(tmp_path, "web"), Status.NEWER_BUILD)])
    c, _ = client_for(result=running)
    assert "Stopped – updating will start it" not in c.get("/").text


def _project_result(tmp_path, *extra):
    a = info(tmp_path, "a", image="redis:7")
    b = info(tmp_path, "b", image="redis:7")
    solo = info(tmp_path, "solo", image="nginx:latest")
    return CheckResult(
        CHECKED_AT,
        [status(a, Status.NEWER_BUILD), status(b, Status.NEWER_BUILD),
         status(solo, Status.NEWER_BUILD), *extra],
    )


def test_whole_project_only_offered_when_image_shared(client_for, tmp_path):
    runner = FakeRunner()
    result = _project_result(tmp_path)
    c, _ = client_for(result=result, runner=runner)
    html = c.get("/").text
    assert _table(html).count("Update whole project") == 2  # a and b share redis:7; solo doesn't
    a_id, solo_id = result.containers[0].container.id, result.containers[2].container.id
    r = c.post(f"/containers/{solo_id}/update", data={"mode": "project", "csrf_token": CSRF})
    assert r.status_code == 400
    r = c.post(f"/containers/{a_id}/update", data={"mode": "project", "csrf_token": CSRF})
    assert r.status_code == 303
    assert [s[0] for s in runner.submitted] == ["project"]


def test_whole_project_refused_when_it_contains_the_dashboard(client_for, tmp_path):
    me = info(tmp_path, "dash", image="ghcr.io/x/dash:latest", is_self=True)
    runner = FakeRunner()
    result = _project_result(tmp_path, status(me, Status.NEWER_BUILD))
    c, _ = client_for(result=result, runner=runner)
    assert "Update whole project" not in c.get("/").text
    a_id = result.containers[0].container.id
    r = c.post(f"/containers/{a_id}/update", data={"mode": "project", "csrf_token": CSRF})
    assert r.status_code == 400
    assert runner.submitted == []


def test_unreadable_stored_check_treated_as_none(client_for, tmp_path, caplog):
    c, app = client_for()
    app.state.store.save_check({"checked_at": "2026-10-17T07:00:00+00:00",
                                "containers": [{"bogus": 1}]}, CHECKED_AT)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        r = c.get("/")
    assert r.status_code == 200
    assert "No containers found." in r.text  # a fresh check ran against the stub docker
    assert "stored check" in caplog.text


@pytest.mark.parametrize("code", ["digest_sent", "digest_nothing", "digest_unconfigured"])
def test_success_flashes_are_not_errors(client_for, seeded, code):
    c, _ = client_for(result=seeded)
    html = c.get(f"/?msg={code}").text
    assert re.search(r'<p class="notice notice-ok" role="status">Digest (sent|skipped)', html)
    assert "notice-error" not in html


def test_error_flash_is_an_alert(client_for, seeded):
    c, _ = client_for(result=seeded)
    html = c.get("/?msg=busy").text
    assert f'<p class="notice notice-error" role="alert">{BUSY_MESSAGE}</p>' in html


async def test_lifespan_closes_own_http_client_even_on_error(settings, tmp_path):
    app = create_app(
        settings, docker_client=StubDocker(), store=Store(str(tmp_path / "l.db")),
        runner=FakeRunner(), start_scheduler=False,
    )
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            raise RuntimeError("shutdown went wrong")
    assert app.state.http.is_closed


def test_server_name_change_shows_without_restart(settings, tmp_path, seeded, monkeypatch):
    monkeypatch.delenv("SERVER_NAME")
    s = Settings(_env_file=None)
    app = make_app(s, tmp_path, seeded)
    c = logged_in_client(app, follow_redirects=False)
    c.__enter__()
    assert "web-new" not in c.get("/").text
    app.state.settings_service.save("general", {"server_name": "web-new", "tz": "Europe/London"}, "me")
    apply_settings(app)
    assert "web-new" in c.get("/").text
    c.__exit__(None, None, None)


def test_health_timeout_read_at_call_time(client_for, seeded, monkeypatch):
    seen = {}

    async def fake_update_version(ctx, info, change, backup_dir, wait, **kw):
        seen.update(kw)

    monkeypatch.setattr("app.main.update_version", fake_update_version)
    monkeypatch.setattr("app.main.run_check", lambda app: asyncio.sleep(0))
    runner = FakeRunner()
    c, app = client_for(result=seeded, runner=runner)
    app.state.settings_service.save(
        "checking",
        {**CHECKING_FORM, "health_timeout_seconds": "77", "mailcow_dir": "/opt/mailcow"},
        "me",
    )
    apply_settings(app)
    db_id = seeded.containers[1].container.id
    sha = preview_sha(c, db_id)
    c.post(f"/containers/{db_id}/version", data={"tag": "16.3", "sha": sha, "csrf_token": CSRF})
    ctx = type("Ctx", (), {"log": staticmethod(lambda t: None)})()
    asyncio.run(runner.submitted[0][3](ctx))
    assert seen["health_timeout"] == 77


def test_apply_settings_writes_helper_config(settings, tmp_path):
    from app.jobs import subprocess_env

    app = make_app(settings, tmp_path)
    app.state.settings_service.save_registry_logins(
        [{"host": "ghcr.io", "username": "me", "password": "s3cret-pw-xyz"}], "me", "added ghcr.io")
    apply_settings(app)
    config_dir = tmp_path / "docker-config"
    cfg = json.loads((config_dir / "config.json").read_text())
    assert cfg == {"credHelpers": {"ghcr.io": "docker-manager"}}
    assert subprocess_env()["DOCKER_CONFIG"] == str(config_dir)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"s3cret-pw-xyz" not in path.read_bytes(), path
    creds = app.state.registry._credentials(parse_image_ref("ghcr.io/o/app:1"))
    assert base64.b64decode(creds) == b"me:s3cret-pw-xyz"


def test_startup_picks_up_saved_logins(settings, tmp_path):
    first = make_app(settings, tmp_path)
    first.state.settings_service.save_registry_logins(
        [{"host": "ghcr.io", "username": "me", "password": "pw"}], "me", "x")
    second = make_app(settings, tmp_path)
    assert second.state.registry._credentials(parse_image_ref("ghcr.io/o/app:1"))
    assert (tmp_path / "docker-config" / "config.json").exists()


# ---- notifications wired to jobs and checks ----

def _notifying_app(settings, tmp_path, provider, events):
    from app.jobs import JobRunner
    from app.notify.channels import Channel

    store = Store(str(tmp_path / "data" / "dashboard.db"))
    app = create_app(
        settings, docker_client=StubDocker(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        store=store, runner=JobRunner(store), start_scheduler=False,
        providers=providers_for(provider))
    seed = CheckResult(CHECKED_AT, [], MailcowStatus(
        update_available=True, current_version="a", latest_release="b"))
    store.save_check(seed.to_dict(), CHECKED_AT)
    app.state.channels.save(
        Channel("", "Ops", "acs", {}, ["a@x.com"], set(events)),
        {"connection_string": "endpoint=https://x;accesskey=k", "sender": "d@x.com"}, "admin")
    return app


async def _run_job(app, fail):
    async def update(ctx, mailcow_dir):
        if fail:
            raise RuntimeError("boom")

    import app.main as main_mod
    main_mod.update_mailcow, original = update, main_mod.update_mailcow
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://testserver") as c:
            sign_in(c, app)
            r = await c.post("/mailcow/update", data={"csrf_token": CSRF})
        assert r.status_code == 303
        await asyncio.wait_for(app.state.runner._task, 5)
    finally:
        main_mod.update_mailcow = original


async def test_failed_job_notifies_subscribed_channel(settings, tmp_path):
    fake = FakeProvider()
    app = _notifying_app(settings, tmp_path, fake, {"job_failed"})
    await _run_job(app, fail=True)
    await asyncio.gather(*app.state.background, return_exceptions=True)
    (_, msg), = fake.sent
    assert "update failed" in msg.subject and "/jobs/1" in msg.plain


async def test_succeeded_job_is_quiet_unless_subscribed(settings, tmp_path):
    fake = FakeProvider()
    app = _notifying_app(settings, tmp_path, fake, {"job_failed"})
    await _run_job(app, fail=False)
    await asyncio.gather(*app.state.background, return_exceptions=True)
    assert fake.sent == []


async def test_hanging_provider_does_not_hold_up_the_job(settings, tmp_path):
    app = _notifying_app(settings, tmp_path, FakeProvider(hang=True), {"job_failed"})
    await _run_job(app, fail=True)  # returns once the job itself is done
    assert app.state.store.get_job(1).status == "failed"
    assert app.state.runner.busy is False
    for task in list(app.state.background):
        task.cancel()
    await asyncio.gather(*app.state.background, return_exceptions=True)


async def test_check_now_notifies_new_updates_once(settings, tmp_path, monkeypatch):
    fake = FakeProvider()
    app = _notifying_app(settings, tmp_path, fake, {"updates_found"})
    web = info(tmp_path, "web", image="nginx:1.25")
    app.state.docker = StubDocker([StubContainer("c1", "web", "nginx:1.25", "sha256:img")])

    async def fake_check_all(containers, *a, **kw):
        return CheckResult(CHECKED_AT, [status(web, minor_update="1.26")])

    monkeypatch.setattr("app.main.check_all", fake_check_all)
    monkeypatch.setattr("app.main.list_containers", lambda docker, self_id: [web])
    from app.main import run_check
    await run_check(app)
    await run_check(app)
    await asyncio.gather(*app.state.background, return_exceptions=True)
    assert len(fake.sent) == 1 and "1 new update" in fake.sent[0][1].subject


async def test_failing_checks_alert_on_second(settings, tmp_path):
    fake = FakeProvider()
    app = _notifying_app(settings, tmp_path, fake, {"check_failing"})
    app.state.docker = BrokenDocker("daemon down")
    from app.main import run_check
    for _ in range(3):
        with pytest.raises(Exception):
            await run_check(app)
    await asyncio.gather(*app.state.background, return_exceptions=True)
    assert len(fake.sent) == 1 and "failing" in fake.sent[0][1].subject


def _rows(html: str) -> list[str]:
    return re.findall(r"<tr data-name[^>]*>", _table(html))


def test_dashboard_rows_and_cards_carry_filter_data(client_for, seeded):
    c, _ = client_for(result=seeded)
    html = c.get("/").text
    rows = _rows(html)
    assert len(rows) == 5  # Mailcow and four containers
    web_row = next(r for r in rows if 'data-name="web"' in r)
    assert 'data-image="nginx:latest"' in web_row
    assert 'data-project="site"' in web_row
    assert 'data-skipped="0"' in web_row and 'data-layout="0"' in web_row
    assert "needs-update" in re.search(r'data-state="([^"]*)"', web_row).group(1).split()
    db_row = next(r for r in rows if 'data-name="db"' in r)
    assert {"needs-update", "major"} <= set(re.search(r'data-state="([^"]*)"', db_row).group(1).split())
    # the attention cards carry the same attributes
    card = re.search(r'<article class="card attention-card"[^>]*data-name="web"[^>]*>', html)
    assert card and 'data-state="' in card.group(0) and 'data-project="site"' in card.group(0)


def test_dashboard_state_tokens_cover_every_kind(client_for, tmp_path):
    items = [
        status(info(tmp_path, "stopped", state="exited")),
        status(info(tmp_path, "cur")),
        status(info(tmp_path, "nocheck"), Status.NOT_CHECKABLE),
        status(info(tmp_path, "nowatch"), Status.NOT_WATCHED),
    ]
    c, _ = client_for(result=CheckResult(checked_at=CHECKED_AT, containers=items))
    html = c.get("/").text
    states = {re.search(r'data-name="([^"]+)"', r).group(1): re.search(r'data-state="([^"]*)"', r).group(1).split()
              for r in _rows(html)}
    assert "stopped" in states["stopped"]
    assert "up-to-date" in states["cur"]
    assert "not-checkable" in states["nocheck"]
    assert "not-watched" in states["nowatch"]


def test_dashboard_chips_are_hidden_buttons(client_for, seeded):
    c, _ = client_for(result=seeded)
    html = c.get("/").text
    tools = re.search(r'<div class="table-tools" id="table-tools"[^>]*>', html).group(0)
    assert " hidden" in tools
    for label in ("Needs update", "Major only", "Errors", "Stopped", "Not checkable", "Skipped"):
        assert re.search(r'<button type="button"[^>]*aria-pressed="false"[^>]*>\s*' + label, html)
    assert "Layout problems" not in html  # no layout data until the layout check exists
    assert 'id="container-search"' in html


def test_dashboard_release_notes_link(client_for, seeded):
    c, _ = client_for(result=seeded)
    html = c.get("/").text
    # web: nginx on Docker Hub, newer build -> library link; opens safely in a new tab
    link = re.search(r'<a [^>]*href="https://hub\.docker\.com/_/nginx/tags"[^>]*>Release notes</a>', html)
    assert link
    assert 'target="_blank"' in link.group(0) and 'rel="noopener noreferrer"' in link.group(0)
    assert "https://hub.docker.com/r/library" not in html


def test_release_notes_link_uses_rule_and_only_for_updates(client_for, tmp_path):
    web = info(tmp_path, "web", labels={"docker-manager.release_notes": "https://example.com/web-notes"})
    cur = info(tmp_path, "cur")
    c, _ = client_for(result=CheckResult(checked_at=CHECKED_AT, containers=[
        status(web, Status.NEWER_BUILD), status(cur)]))
    html = c.get("/").text
    assert 'href="https://example.com/web-notes"' in html
    assert html.count(">Release notes</a>") == 2  # row and card of web; none for the current one
