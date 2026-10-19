"""Profile photos: Microsoft at sign-in, Gravatar for everyone else, cached for a day."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import photos
from app.main import create_app
from app.photos import GRAPH_PHOTO_URL, PhotoUnavailable, fetch_image, gravatar_url
from app.roles import Role
from app.store import Store
from auth_helpers import sign_in
from test_entra import claims_for

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 100
HTML = {"Accept": "text/html"}


@pytest.fixture
def app(settings, tmp_path):
    app = create_app(
        settings, docker_client=object(), http=httpx.AsyncClient(),
        store=Store(str(tmp_path / "data" / "dashboard.db")), start_scheduler=False,
    )
    yield app
    app.state.store.close()


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


def image(data=JPEG, content_type="image/jpeg", status=200):
    return httpx.Response(status, content=data, headers={"content-type": content_type})


def entra_sign_in(client, app, settings, access_token="graph-token"):
    app.state.oauth.entra.authorize_access_token = AsyncMock(return_value={
        "userinfo": claims_for(settings), "access_token": access_token})
    r = client.get("/auth/callback?code=1&state=2", headers=HTML)
    assert r.status_code in (302, 303)
    return app.state.users.by_email("entra", "alice@example.com")


# ---- fetching -----------------------------------------------------------------

@respx.mock
async def test_fetch_image_accepts_only_real_jpeg_or_png():
    url = "https://img.example/x"
    async with httpx.AsyncClient() as http:
        respx.get(url).mock(return_value=image())
        assert await fetch_image(http, url) == ("image/jpeg", JPEG)
        respx.get(url).mock(return_value=image(PNG, "image/png; charset=binary"))
        assert await fetch_image(http, url) == ("image/png", PNG)
        respx.get(url).mock(return_value=image(status=404))
        assert await fetch_image(http, url) is None
        for bad in (image(status=500), image(b"<svg/>", "image/svg+xml"),
                    image(PNG, "image/jpeg"), image(b"\xff\xd8\xff" + b"x" * 300_000)):
            respx.get(url).mock(return_value=bad)
            with pytest.raises(PhotoUnavailable):
                await fetch_image(http, url)
        respx.get(url).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(PhotoUnavailable):
            await fetch_image(http, url)


def test_gravatar_url_hashes_the_trimmed_lower_case_email():
    assert gravatar_url(" Ann@Example.com ") == gravatar_url("ann@example.com")
    assert "ann@example.com" not in gravatar_url("ann@example.com")
    assert gravatar_url("a@b.c").endswith("?s=96&d=404")


# ---- Microsoft sign-in --------------------------------------------------------

@respx.mock
def test_microsoft_photo_is_stored_and_served(client, app, settings):
    graph = respx.get(GRAPH_PHOTO_URL).mock(return_value=image())
    rec = entra_sign_in(client, app, settings)
    assert graph.calls.last.request.headers["authorization"] == "Bearer graph-token"
    assert rec.photo_source == "microsoft" and rec.photo_etag
    page = client.get("/account", headers=HTML).text
    assert f'<img src="/photos/{rec.id}?v={rec.photo_etag}"' in page
    r = client.get(f"/photos/{rec.id}")
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age=86400" in r.headers["cache-control"]


@respx.mock
def test_no_microsoft_photo_falls_back_to_gravatar(client, app, settings):
    respx.get(GRAPH_PHOTO_URL).mock(return_value=image(status=404))
    respx.get(gravatar_url("alice@example.com")).mock(return_value=image(PNG, "image/png"))
    rec = entra_sign_in(client, app, settings)
    assert rec.photo_source == "gravatar"
    assert app.state.users.photo(rec.id) == ("image/png", PNG)


@respx.mock
def test_graph_failure_never_blocks_sign_in(client, app, settings):
    respx.get(GRAPH_PHOTO_URL).mock(return_value=image(status=403))
    rec = entra_sign_in(client, app, settings)
    assert rec.photo_etag is None and rec.photo_source is None


@respx.mock
def test_removed_microsoft_photo_is_dropped(client, app, settings):
    respx.get(GRAPH_PHOTO_URL).mock(return_value=image())
    entra_sign_in(client, app, settings)
    respx.get(GRAPH_PHOTO_URL).mock(return_value=image(status=404))
    respx.get(gravatar_url("alice@example.com")).mock(return_value=image(status=404))
    rec = entra_sign_in(client, app, settings)
    assert rec.photo_source == "none" and rec.photo_etag is None
    assert app.state.users.photo(rec.id) is None


# ---- Gravatar and the day-long cache --------------------------------------------

@respx.mock
async def test_gravatar_answers_are_kept_for_a_day(app):
    now = [datetime(2026, 10, 19, 12, tzinfo=timezone.utc)]
    app.state.photos._clock = lambda: now[0]
    users = app.state.users
    rec = users.create("local", "ann@example.com", "Ann", "viewer")
    assert app.state.photos.due(rec)
    route = respx.get(gravatar_url("ann@example.com")).mock(return_value=image(status=404))
    await app.state.photos.from_gravatar(rec)
    rec = users.get(rec.id)
    assert rec.photo_source == "none" and not app.state.photos.due(rec)
    now[0] += timedelta(hours=23)
    assert not app.state.photos.due(rec)
    now[0] += timedelta(hours=1)
    assert app.state.photos.due(rec)
    route.mock(return_value=image())
    await app.state.photos.from_gravatar(rec)
    rec = users.get(rec.id)
    assert rec.photo_source == "gravatar" and rec.photo_etag
    # A failed lookup keeps the picture and waits another day.
    now[0] += timedelta(days=2)
    route.mock(side_effect=httpx.ConnectError("down"))
    await app.state.photos.from_gravatar(rec)
    after = users.get(rec.id)
    assert after.photo_etag == rec.photo_etag and after.photo_checked_at == now[0]
    assert not app.state.photos.due(after)


def test_gravatar_setting_off_means_no_lookups(app):
    rec = app.state.users.create("local", "ann@example.com", "Ann", "viewer")
    assert app.state.photos.due(rec)
    app.state.settings = app.state.settings.model_copy(update={"gravatar": False})
    assert not app.state.photos.due(rec)


@respx.mock
async def test_showing_an_avatar_starts_a_background_lookup(app, monkeypatch):
    monkeypatch.setattr(photos, "LOOKUPS_ENABLED", True)
    route = respx.get(gravatar_url("ann@example.com")).mock(return_value=image())
    rec = app.state.users.create("local", "ann@example.com", "Ann", "viewer")
    assert app.state.photos.url(rec) is None  # initials for now
    app.state.photos.url(rec)  # a second avatar on the page doesn't look up twice
    for _ in range(20):
        await asyncio.sleep(0)
        if app.state.users.get(rec.id).photo_etag:
            break
    assert route.call_count == 1
    rec = app.state.users.get(rec.id)
    assert app.state.photos.url(rec) == f"/photos/{rec.id}?v={rec.photo_etag}"


# ---- serving --------------------------------------------------------------------

def test_photo_route_shows_others_only_to_admins(app):
    users = app.state.users
    ann = users.create("local", "ann@example.com", "Ann", "viewer")
    users.set_photo(ann.id, "gravatar", "image/jpeg", JPEG,
                    checked_at=datetime.now(timezone.utc))
    with TestClient(app, base_url="https://testserver") as c:
        sign_in(c, app, Role.OPERATOR)
        assert c.get(f"/photos/{ann.id}").status_code == 404
        sign_in(c, app, Role.ADMIN)
        assert c.get(f"/photos/{ann.id}").content == JPEG
        html = c.get("/users", headers=HTML).text
        assert f'src="/photos/{ann.id}?v=' in html
        assert c.get("/photos/99999").status_code == 404


def test_deleting_a_user_deletes_their_photo(app):
    users = app.state.users
    ann = users.create("local", "ann@example.com", "Ann", "viewer")
    users.set_photo(ann.id, "gravatar", "image/jpeg", JPEG,
                    checked_at=datetime.now(timezone.utc))
    users.delete(ann.id)
    assert users.photo(ann.id) is None
