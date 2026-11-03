"""The Images page: what it lists, who sees the buttons, and what Delete and Tidy submit."""
import asyncio
import re
from dataclasses import replace

import docker.errors
import pytest

from app.checker import Status
from app.roles import Role
from auth_helpers import CSRF, logged_in_client
from test_images import FakeImage, LogCtx, RemovableImages
from test_routes import (  # noqa: F401 – fixtures
    FakeRunner, StubContainer, StubContainers, client_for, compose_file, info, make_app, seeded,
    status,
)

GB = 1_000_000_000
IN_USE = "sha256:img"  # the seeded web container's image (its old nginx:latest)
WAITING = "sha256:" + "e" * 64  # nginx:latest now: downloaded ahead for web's newer build
REDIS = "sha256:" + "a" * 64
UNTAGGED = "sha256:" + "0123456789ab" + "0" * 52


class ImagesDocker:
    def __init__(self):
        self.images = ListedImages([
            FakeImage(IN_USE, [], 1 * GB),
            FakeImage(WAITING, ["nginx:latest"], 3 * GB),
            FakeImage(REDIS, ["redis:7.0"], 2 * GB),
            FakeImage(UNTAGGED, [], 500_000_000),
        ])
        self.containers = StubContainers([StubContainer("w" * 64, "web", "nginx:latest", IN_USE)])


class ListedImages(RemovableImages):
    def list(self):
        return list(self.images.values())


class BrokenImages:
    def list(self):
        raise docker.errors.APIError("docker is down")


@pytest.fixture
def images_client(client_for, seeded):  # noqa: F811
    def factory(**kw):
        kw.setdefault("docker_client", ImagesDocker())
        return client_for(result=seeded, **kw)
    return factory


def _text(html: str) -> str:
    return html.replace("<wbr>", "")  # long names may wrap after : and /


def _unused(html: str) -> str:
    return html[html.index('id="unused-heading"'):html.index('id="waiting-heading"')]


def _waiting(html: str) -> str:
    return html[html.index('id="waiting-heading"'):]


def test_images_page_lists_unused_and_waiting(images_client):
    c, _ = images_client()
    r = c.get("/images")
    assert r.status_code == 200
    html = _text(r.text)
    assert "Unused images · 2 · 2.5 GB" in html
    unused = _unused(html)
    assert "redis:7.0" in unused and "untagged 0123456789ab" in unused
    assert "2.0 GB" in unused and "500 MB" in unused
    assert unused.index("redis:7.0") < unused.index("untagged 0123456789ab")  # biggest first
    assert "before " in unused  # first scan: the real time isn't known
    assert f'action="/images/{REDIS}/delete"' in unused
    assert 'action="/images/tidy"' in html and "Tidy all unused" in html
    assert 'data-confirm-detail="This frees 2.5 GB.' in html  # the confirm says what's freed
    waiting = _waiting(html)
    assert "Waiting to be applied" in waiting and "nginx:latest" in waiting
    assert "/delete" not in waiting  # never deletable from here
    assert IN_USE not in html  # a container uses it: not listed at all
    assert 'id="confirm-dialog"' in html


def test_images_page_empty_state(client_for, seeded):  # noqa: F811
    c, _ = client_for(result=seeded)  # the stub Docker has no images
    html = c.get("/images").text
    assert "No unused images." in html
    assert "Tidy all unused" not in html


def test_viewer_sees_no_delete_buttons(images_client):
    _, app = images_client()
    with logged_in_client(app, Role.VIEWER) as viewer:
        html = _text(viewer.get("/images").text)
    assert "redis:7.0" in html
    assert "/delete" not in html and "Tidy all unused" not in html and "/images/tidy" not in html


def test_images_page_shows_docker_error(images_client):
    docker_client = ImagesDocker()
    docker_client.images = BrokenImages()
    c, _ = images_client(docker_client=docker_client)
    r = c.get("/images")
    assert r.status_code == 200
    assert "Couldn&#39;t read the images from Docker" in r.text


def test_delete_of_waiting_or_used_image_is_404(images_client):
    runner = FakeRunner()
    c, _ = images_client(runner=runner)
    for image_id in (WAITING, IN_USE, "sha256:" + "f" * 64):
        r = c.post(f"/images/{image_id}/delete", data={"csrf_token": CSRF})
        assert r.status_code == 404, image_id
    assert runner.submitted == []


def test_delete_submits_a_tidy_job_for_one_image(images_client, monkeypatch):
    monkeypatch.setattr("app.main.run_check", lambda app: asyncio.sleep(0))
    runner = FakeRunner()
    c, app = images_client(runner=runner)
    r = c.post(f"/images/{REDIS}/delete", data={"csrf_token": CSRF})  # the id has a colon
    assert r.status_code == 303 and r.headers["location"] == "/jobs/7"
    kind, target, _, work = runner.submitted[0]
    assert (kind, target) == ("tidy", "redis:7.0")
    asyncio.run(work(LogCtx()))
    docker_images = app.state.docker.images
    assert docker_images.removed == ["redis:7.0"]
    assert set(docker_images.images) == {IN_USE, WAITING, UNTAGGED}


def test_tidy_submits_one_job_with_every_unused_image(images_client, monkeypatch):
    monkeypatch.setattr("app.main.run_check", lambda app: asyncio.sleep(0))
    runner = FakeRunner()
    c, app = images_client(runner=runner)
    checked_at = app.state.store.latest_check()[0]
    r = c.post("/images/tidy", data={"csrf_token": CSRF})
    assert r.status_code == 303 and r.headers["location"] == "/jobs/7"
    assert len(runner.submitted) == 1
    kind, target, _, work = runner.submitted[0]
    assert (kind, target) == ("tidy", "2 unused images")
    after_check = []
    app.state.notifier.after_check = lambda *a: after_check.append(a)
    ctx = LogCtx()
    asyncio.run(work(ctx))
    docker_images = app.state.docker.images
    assert set(docker_images.images) == {IN_USE, WAITING}  # protected and in use stay
    assert "Freed 2.5 GB\n" in ctx.logs
    # The stored check's card is current at once – same check, no notifications.
    when, payload = app.state.store.latest_check()
    assert when == checked_at
    assert payload["unused_images"] == {"count": 0, "size": 0}
    assert after_check == []


def test_tidy_skips_an_image_waiting_to_be_applied_since_the_page(images_client, seeded,  # noqa: F811
                                                                  tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.run_check", lambda app: asyncio.sleep(0))
    runner = FakeRunner()
    c, app = images_client(runner=runner)
    c.post("/images/tidy", data={"csrf_token": CSRF})
    _, _, _, work = runner.submitted[0]
    # A check since the page was shown: redis:7.0 is now a newer build on offer for cache.
    cache = info(tmp_path, "cache", image="redis:7.0", image_id="sha256:oldredis")
    later = replace(seeded, containers=[*seeded.containers, status(cache, Status.NEWER_BUILD)])
    app.state.store.save_check(later.to_dict(), later.checked_at)
    ctx = LogCtx()
    asyncio.run(work(ctx))
    assert set(app.state.docker.images.images) == {IN_USE, WAITING, REDIS}
    assert "Skipped redis:7.0 – now waiting to be applied\n" in ctx.logs
    assert "Deleted untagged 0123456789ab (500 MB)\n" in ctx.logs


def test_tidy_with_nothing_unused_says_so(client_for, seeded):  # noqa: F811
    runner = FakeRunner()
    c, _ = client_for(result=seeded, runner=runner)
    r = c.post("/images/tidy", data={"csrf_token": CSRF})
    assert r.status_code == 303 and r.headers["location"] == "/images?msg=tidy_nothing"
    assert runner.submitted == []
    assert "Nothing to tidy" in c.get("/images?msg=tidy_nothing").text


def test_viewer_cannot_post_tidy_or_delete(images_client):
    runner = FakeRunner()
    _, app = images_client(runner=runner)
    with logged_in_client(app, Role.VIEWER, follow_redirects=False) as viewer:
        assert viewer.post("/images/tidy", data={"csrf_token": CSRF}).status_code == 403
        r = viewer.post(f"/images/{REDIS}/delete", data={"csrf_token": CSRF})
        assert r.status_code == 403
    assert runner.submitted == []


def test_tidy_needs_csrf(images_client):
    runner = FakeRunner()
    c, _ = images_client(runner=runner)
    assert c.post("/images/tidy").status_code == 403
    assert runner.submitted == []


def test_with_no_stored_check_the_page_checks_first(client_for, seeded, monkeypatch):  # noqa: F811
    # Without a check nothing would be protected: the download for web would show as unused.
    async def fake_run_check(app):
        app.state.store.save_check(seeded.to_dict(), seeded.checked_at)
        return seeded

    monkeypatch.setattr("app.main.run_check", fake_run_check)
    runner = FakeRunner()
    c, _ = client_for(docker_client=ImagesDocker(), runner=runner)
    html = _text(c.get("/images").text)
    assert "nginx:latest" in _waiting(html) and "nginx:latest" not in _unused(html)
    assert c.post(f"/images/{WAITING}/delete", data={"csrf_token": CSRF}).status_code == 404


def test_with_no_check_possible_nothing_is_deleted(client_for, monkeypatch):
    async def failing_check(app):
        raise RuntimeError("docker is down")

    monkeypatch.setattr("app.main.run_check", failing_check)
    runner = FakeRunner()
    c, _ = client_for(docker_client=ImagesDocker(), runner=runner)
    r = c.get("/images")
    assert r.status_code == 200 and "The check could not be completed" in r.text
    assert "redis:7.0" not in r.text
    for path in ("/images/tidy", f"/images/{REDIS}/delete"):
        r = c.post(path, data={"csrf_token": CSRF})
        assert r.status_code == 303 and r.headers["location"] == "/images?msg=check_failed"
    assert runner.submitted == []


def test_nav_has_images_between_layout_and_users(client_for, seeded):  # noqa: F811
    c, _ = client_for(result=seeded)
    nav = re.search(r'<nav class="site-nav".*?</nav>', c.get("/images").text, re.S).group(0)
    links = re.findall(r'<a href="(/[a-z]*)"', nav)
    assert links == ["/", "/history", "/layout", "/images", "/users", "/settings"]
    assert '<a href="/images" aria-current="page">' in nav


def test_job_kind_label():
    from app.main import JOB_KIND_LABELS
    assert JOB_KIND_LABELS["tidy"] == "Tidy images"


def test_tidy_confirm_wording(images_client):
    c, _ = images_client()
    html = c.get("/images").text
    assert 'data-confirm="Delete all 2 unused images?"' in html
    assert "Images a container starts using in the meantime are skipped." in html
