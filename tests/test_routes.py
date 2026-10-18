import asyncio
import logging
import re
from datetime import datetime, timezone

import docker.errors
import httpx
import pytest

from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status
from app.inventory import ContainerInfo
from app.jobs import BUSY_MESSAGE, BusyError
from app.main import create_app
from app.store import Store
from auth_helpers import CSRF, USER, assert_all_routes_guarded, logged_in_client, set_session

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

    async def subscribe(self, job_id):
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
        c = logged_in_client(app, settings, follow_redirects=False)
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
    assert f"/containers/{lone_id}/" not in html
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
    assert "Updating the dashboard – reload in a minute" in r.text
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
        set_session(c, app, {"user": USER, "csrf": CSRF})
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
    with logged_in_client(app, settings, follow_redirects=False) as c:
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


class DigestClient:
    def __init__(self):
        self.messages = []

    def begin_send(self, message):
        self.messages.append(message)

        class Poller:
            def result(self_inner):
                return {}

        return Poller()


def digest_app(settings, tmp_path, factory, result=None, **overrides):
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
        email_client_factory=factory,
    )
    c = logged_in_client(app, s, follow_redirects=False)
    c.__enter__()
    return c


def test_digest_send_sends_latest_check(settings, tmp_path, seeded):
    fake = DigestClient()
    c = digest_app(settings, tmp_path, lambda conn: fake, seeded)
    r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.status_code == 303 and r.headers["location"] == "/?msg=digest_sent"
    assert fake.messages[0]["content"]["subject"] == "test-server: 5 updates available"
    assert "Digest sent" in c.get("/?msg=digest_sent").text
    c.__exit__(None, None, None)


def test_digest_send_runs_check_when_none_stored(settings, tmp_path):
    fake = DigestClient()
    c = digest_app(settings, tmp_path, lambda conn: fake)
    r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_sent"
    assert fake.messages[0]["content"]["subject"] == "test-server: all up to date"
    c.__exit__(None, None, None)


def test_digest_send_nothing_to_report(settings, tmp_path):
    fake = DigestClient()
    c = digest_app(settings, tmp_path, lambda conn: fake, digest_when_none=False)
    r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_nothing"
    assert fake.messages == []
    assert "nothing to report" in c.get("/?msg=digest_nothing").text
    c.__exit__(None, None, None)


def test_digest_send_not_configured(client_for, seeded):
    c, _ = client_for(result=seeded)
    r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_unconfigured"
    assert "email not configured" in c.get("/?msg=digest_unconfigured").text


def test_digest_send_failure_logged(settings, tmp_path, seeded, caplog):
    def factory(conn):
        raise RuntimeError("acs down")

    c = digest_app(settings, tmp_path, factory, seeded)
    with caplog.at_level(logging.ERROR, logger="app.main"):
        r = c.post("/digest/send", data={"csrf_token": CSRF})
    assert r.headers["location"] == "/?msg=digest_failed"
    assert "Sending the digest failed" in caplog.text and "acs down" in caplog.text
    assert "could not be sent" in c.get("/?msg=digest_failed").text
    c.__exit__(None, None, None)


def test_digest_send_requires_csrf(settings, tmp_path, seeded):
    fake = DigestClient()
    c = digest_app(settings, tmp_path, lambda conn: fake, seeded)
    assert c.post("/digest/send", data={"csrf_token": "wrong"}).status_code == 403
    assert fake.messages == []
    c.__exit__(None, None, None)


def test_digest_button_only_when_enabled(client_for, settings, tmp_path, seeded):
    c, _ = client_for(result=seeded)
    assert "/digest/send" not in c.get("/").text
    c2 = digest_app(settings, tmp_path, None, seeded)
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
