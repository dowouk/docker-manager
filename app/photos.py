"""Profile photos: the Microsoft photo, fetched at sign-in, or a Gravatar picture for anyone
without one (all local accounts, and Microsoft users with no photo set).

The dashboard fetches and serves them itself, so browsers never contact Microsoft or
Gravatar. A Gravatar answer – a picture or "none" – is kept for 24 hours, then looked up
again in the background the next time that person's avatar is shown.
"""
import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response

from app.auth import User, current_user
from app.roles import Role
from app.users import UserRecord, UserStore

log = logging.getLogger(__name__)

GRAPH_PHOTO_URL = "https://graph.microsoft.com/v1.0/me/photos/96x96/$value"
GRAVATAR_URL = "https://gravatar.com/avatar/{}?s=96&d=404"
CACHE_FOR = timedelta(hours=24)
MAX_PHOTO_BYTES = 256 * 1024
# Only these are stored and served, and the bytes must match the type.
_TYPES = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
# Background Gravatar lookups; the test suite turns them off so it never reaches the network.
LOOKUPS_ENABLED = True


class PhotoUnavailable(Exception):
    """The photo couldn't be checked (network, HTTP error, or not a usable image)."""


async def fetch_image(http: httpx.AsyncClient, url: str,
                      headers: dict | None = None) -> tuple[str, bytes] | None:
    """(content type, bytes), or None when there's no picture (HTTP 404)."""
    try:
        resp = await http.get(url, headers=headers or {}, timeout=5)
    except httpx.HTTPError as exc:
        raise PhotoUnavailable(type(exc).__name__) from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise PhotoUnavailable(f"HTTP {resp.status_code}")
    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    data = resp.content
    magic = _TYPES.get(content_type)
    if magic is None or not data.startswith(magic) or len(data) > MAX_PHOTO_BYTES:
        raise PhotoUnavailable("not a JPEG or PNG under the size limit")
    return content_type, data


def gravatar_url(email: str) -> str:
    return GRAVATAR_URL.format(hashlib.sha256(email.strip().lower().encode()).hexdigest())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Photos:
    def __init__(self, users: Callable[[], UserStore], http: Callable[[], httpx.AsyncClient],
                 enabled: Callable[[], bool], clock: Callable[[], datetime] = _utcnow) -> None:
        self._users = users
        self._http = http
        self._enabled = enabled
        self._clock = clock
        self._pending: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def due(self, rec: UserRecord) -> bool:
        """A Gravatar lookup is due (never for someone with a Microsoft photo)."""
        if rec.photo_source == "microsoft" or not self._enabled():
            return False
        return rec.photo_checked_at is None or self._clock() - rec.photo_checked_at >= CACHE_FOR

    async def from_microsoft(self, rec: UserRecord, access_token: str | None) -> None:
        """After a Microsoft sign-in: store their photo, or fall back to Gravatar."""
        if not access_token:
            return
        try:
            photo = await fetch_image(self._http(), GRAPH_PHOTO_URL,
                                      {"Authorization": f"Bearer {access_token}"})
        except PhotoUnavailable as exc:
            log.info("Couldn't fetch the Microsoft photo for %s (%s)", rec.email, exc)
            return
        users = self._users()
        if photo is not None:
            users.set_photo(rec.id, "microsoft", *photo, checked_at=self._clock())
            return
        if rec.photo_source == "microsoft":  # they removed it: look on Gravatar instead
            users.clear_photo(rec.id, source=None, checked_at=None)
            rec = users.get(rec.id) or rec
        if self.due(rec):
            await self.from_gravatar(rec)

    async def from_gravatar(self, rec: UserRecord) -> None:
        users = self._users()
        now = self._clock()
        try:
            photo = await fetch_image(self._http(), gravatar_url(rec.email))
        except PhotoUnavailable as exc:
            # Keep whatever is stored and try again tomorrow, not on every page.
            log.info("Couldn't check Gravatar for %s (%s)", rec.email, exc)
            users.touch_photo_check(rec.id, now)
            return
        if photo is None:
            users.clear_photo(rec.id, source="none", checked_at=now)
        else:
            users.set_photo(rec.id, "gravatar", *photo, checked_at=now)

    def refresh_soon(self, rec: UserRecord) -> None:
        """Start a background Gravatar lookup if one is due; the page doesn't wait for it."""
        if not LOOKUPS_ENABLED or not self.due(rec) or rec.id in self._pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # rendering in a worker thread
            loop = self.loop
        if loop is None or loop.is_closed():
            return
        self._pending.add(rec.id)
        loop.call_soon_threadsafe(self._start, rec)

    def _start(self, rec: UserRecord) -> None:
        task = asyncio.ensure_future(self._run(rec))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, rec: UserRecord) -> None:
        try:
            await self.from_gravatar(rec)
        except Exception as exc:  # noqa: BLE001 – a photo is never worth an error
            log.warning("Gravatar lookup failed for %s (%s)", rec.email, type(exc).__name__)
        finally:
            self._pending.discard(rec.id)

    def url(self, person: User | UserRecord | None) -> str | None:
        """The avatar image URL, or None for initials. Starts a lookup if one is due."""
        if person is None:
            return None
        rec = person if isinstance(person, UserRecord) else self._users().get(person.id)
        if rec is None:
            return None
        self.refresh_soon(rec)
        return f"/photos/{rec.id}?v={rec.photo_etag}" if rec.photo_etag else None


def setup_photos(app: FastAPI) -> None:
    @app.get("/photos/{user_id}", include_in_schema=False)
    async def photo(user_id: int, user: User = Depends(current_user)):
        # Your own picture, or anyone's for an Admin (the Users pages).
        if user_id != user.id and user.role < Role.ADMIN:
            raise HTTPException(status_code=404)
        found = app.state.users.photo(user_id)
        if found is None:
            raise HTTPException(status_code=404)
        content_type, data = found
        # The URL carries the photo's etag, so a changed photo is a new URL.
        return Response(data, media_type=content_type,
                        headers={"Cache-Control": "private, max-age=86400"})
