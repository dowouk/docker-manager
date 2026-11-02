"""Images on the server that no container uses, and the ones held back for a pending update."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import docker.errors

from app.imageref import parse_image_ref
from app.store import Store

if TYPE_CHECKING:
    from app.checker import CheckResult
    from app.jobs import JobContext


@dataclass(frozen=True)
class ImageRow:
    id: str  # full "sha256:…"
    tags: list[str]  # RepoTags without "<none>:<none>"; [] when untagged
    size: int  # bytes
    unused_since: datetime | None
    before: bool  # already unused at the first scan: shown as "before <date>"

    @property
    def name(self) -> str:
        return ", ".join(self.tags) or f"untagged {self.id.removeprefix('sha256:')[:12]}"


@dataclass(frozen=True)
class UnusedSummary:
    count: int
    size: int  # bytes


@dataclass
class ImagesReport:
    unused: list[ImageRow]
    waiting: list[ImageRow]  # protected: downloaded for an update still on offer

    def summary(self) -> UnusedSummary:
        return UnusedSummary(len(self.unused), sum(r.size for r in self.unused))


def protected_ids(visible: CheckResult, image_id_for: Callable[[str], str | None]) -> set[str]:
    """Image ids downloaded for an update that is still on offer (skips already applied)."""
    from app.checker import Status  # imported here: checker imports this module

    protected: set[str] = set()
    for cs in visible.containers:
        c = cs.container
        if cs.status in (Status.NEWER_BUILD, Status.PULLED_NOT_RECREATED):
            # A live lookup: a download that finished since the check still shows NEWER_BUILD.
            found = image_id_for(c.image_ref)
            if found and found != c.image_id:
                protected.add(found)
        if cs.minor_update:
            found = image_id_for(parse_image_ref(c.image_ref).with_tag(cs.minor_update))
            if found:
                protected.add(found)
    return protected


def _image_id_for(client) -> Callable[[str], str | None]:
    def lookup(ref: str) -> str | None:
        try:
            return client.images.get(ref).id
        except docker.errors.APIError:  # includes ImageNotFound
            return None

    return lookup


def scan(client, store: Store, visible: CheckResult | None, now: datetime) -> ImagesReport:
    images = client.images.list()
    used = {c.attrs["Image"] for c in client.containers.list(all=True)}
    protected = protected_ids(visible, _image_id_for(client)) if visible else set()
    candidates = [i for i in images if i.id not in used]
    unused_images = [i for i in candidates if i.id not in protected]
    since = store.sync_unused_images({i.id for i in unused_images}, now)

    def row(image, tracked: bool) -> ImageRow:
        when, before = since[image.id] if tracked else (None, False)
        return ImageRow(image.id, _tags(image), image.attrs.get("Size") or 0, when, before)

    return ImagesReport(
        unused=sorted((row(i, True) for i in unused_images), key=lambda r: -r.size),
        waiting=sorted((row(i, False) for i in candidates if i.id in protected),
                       key=lambda r: -r.size),
    )


def _tags(image) -> list[str]:
    return [t for t in image.attrs.get("RepoTags") or [] if t != "<none>:<none>"]


def _short(image_id: str) -> str:
    return image_id.removeprefix("sha256:")[:12]


async def delete_images(ctx: JobContext, client, store: Store, ids: list[str]) -> None:
    """Delete each image by removing its tags, then the image itself – never forced, so
    Docker refuses one a container uses. A refused image is skipped and the rest go ahead."""
    used = {c.attrs["Image"] for c in await asyncio.to_thread(client.containers.list, all=True)}
    gone: set[str] = set()
    total = 0
    for image_id in ids:
        try:
            image = await asyncio.to_thread(client.images.get, image_id)
        except docker.errors.ImageNotFound:
            ctx.log(f"Already deleted {_short(image_id)}\n")
            gone.add(image_id)
            continue
        except docker.errors.APIError as exc:
            ctx.log(f"Skipped {_short(image_id)} – {exc.explanation or exc}\n")
            continue
        size = image.attrs.get("Size") or 0
        name = ImageRow(image.id, _tags(image), size, None, False).name
        if image.id in used:  # a container started using it since the page was shown
            ctx.log(f"Skipped {name} – a container uses it\n")
            continue
        try:
            for tag in _tags(image):
                # Only while the tag still points here: a tag pulled again since names a
                # different image, and removing it would untag that one.
                try:
                    points_here = (await asyncio.to_thread(client.images.get, tag)).id == image.id
                except docker.errors.ImageNotFound:
                    points_here = False
                if points_here:
                    await asyncio.to_thread(client.images.remove, tag)
            try:
                await asyncio.to_thread(client.images.get, image.id)
            except docker.errors.ImageNotFound:
                pass  # the last tag took the image with it
            else:
                await asyncio.to_thread(client.images.remove, image.id)
        except docker.errors.APIError as exc:  # e.g. 409: a container uses it
            ctx.log(f"Skipped {name} – {exc.explanation or exc}\n")
            continue
        ctx.log(f"Deleted {name} ({human_size(size)})\n")
        gone.add(image_id)
        total += size
    store.forget_unused_images(gone)
    ctx.log(f"Freed {human_size(total)}\n")


def human_size(n: int) -> str:
    """Decimal units; one decimal place from GB up."""
    if n < 1000:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 1000 or unit == "TB":
            return f"{value:.1f} {unit}" if unit in ("GB", "TB") else f"{round(value)} {unit}"
    raise AssertionError("unreachable")
