"""Images on the server that no container uses, and the ones held back for a pending update."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import docker.errors

from app.imageref import parse_image_ref
from app.store import Store

if TYPE_CHECKING:
    from app.checker import CheckResult


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
        tags = [t for t in image.attrs.get("RepoTags") or [] if t != "<none>:<none>"]
        return ImageRow(image.id, tags, image.attrs.get("Size") or 0, when, before)

    return ImagesReport(
        unused=sorted((row(i, True) for i in unused_images), key=lambda r: -r.size),
        waiting=sorted((row(i, False) for i in candidates if i.id in protected),
                       key=lambda r: -r.size),
    )


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
