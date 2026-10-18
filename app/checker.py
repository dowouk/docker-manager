"""Combine inventory, registry and version data into per-container statuses."""
import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from app.config import Settings
from app.imageref import parse_image_ref
from app.inventory import ContainerInfo, Mount, is_mailcow
from app.layout import LayoutProblem
from app.registry import NotFoundError, RegistryClient
from app.rules import Rules, TagFilter, tag_executor
from app.versions import find_version_updates, parse_version_tag

_UPDATABLE_STATES = ("running", "exited")


class Status(StrEnum):
    UP_TO_DATE = "up_to_date"
    NEWER_BUILD = "newer_build"
    PULLED_NOT_RECREATED = "pulled_not_recreated"
    NOT_CHECKABLE = "not_checkable"
    ERROR = "error"
    NOT_WATCHED = "not_watched"


@dataclass
class MailcowStatus:
    update_available: bool | None = None
    current_version: str | None = None
    latest_release: str | None = None
    error: str | None = None


@dataclass
class ContainerStatus:
    container: ContainerInfo
    status: Status
    remote_digest: str | None = None
    minor_update: str | None = None
    major_update: str | None = None
    message: str | None = None
    updatable: bool = True
    not_updatable_reason: str | None = None
    rule_warnings: list[str] = field(default_factory=list)
    notify: bool = True


def _container_from_dict(data: dict) -> ContainerInfo:
    # Older stored results lack labels, mounts and image_source: the defaults apply.
    mounts = [Mount(**m) for m in data.get("mounts") or []]
    return ContainerInfo(**{**data, "mounts": mounts})


@dataclass
class CheckResult:
    checked_at: datetime
    containers: list[ContainerStatus]
    mailcow: MailcowStatus | None = None
    layout: list[LayoutProblem] | None = None  # None: no layout check has run

    def to_dict(self) -> dict:
        return {
            "checked_at": self.checked_at.isoformat(),
            "containers": [
                {**asdict(cs), "status": cs.status.value} for cs in self.containers
            ],
            "mailcow": asdict(self.mailcow) if self.mailcow else None,
            "layout": None if self.layout is None else [asdict(p) for p in self.layout],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CheckResult":
        containers = [
            ContainerStatus(
                **{
                    **cs,
                    "container": _container_from_dict(cs["container"]),
                    "status": Status(cs["status"]),
                }
            )
            for cs in data["containers"]
        ]
        mailcow = MailcowStatus(**data["mailcow"]) if data.get("mailcow") else None
        layout = None if data.get("layout") is None else [LayoutProblem(**p) for p in data["layout"]]
        return cls(datetime.fromisoformat(data["checked_at"]), containers, mailcow, layout)

    def summary(self) -> dict[str, int]:
        updates = majors = errors = up_to_date = 0
        for cs in self.containers:
            if cs.major_update:
                majors += 1
            if cs.status in (Status.NOT_CHECKABLE, Status.NOT_WATCHED):
                continue  # local, pinned or unwatched: neither current nor a problem
            if cs.status == Status.ERROR:
                errors += 1
            elif (
                cs.status in (Status.NEWER_BUILD, Status.PULLED_NOT_RECREATED)
                or cs.minor_update
            ):
                updates += 1
            elif cs.status == Status.UP_TO_DATE:
                up_to_date += 1
        if self.mailcow:
            if self.mailcow.error:
                errors += 1
            elif self.mailcow.update_available:
                updates += 1
        return {"up_to_date": up_to_date, "updates": updates, "major": majors, "errors": errors}


@dataclass
class _Remote:
    digest: str | None = None
    digest_error: str | None = None
    not_found: bool = False
    tags: list[str] | None = None
    tags_error: str | None = None


def _sha(digest: str) -> str:
    return digest.rsplit("@", 1)[-1]


def _is_mailcow(c: ContainerInfo, settings: Settings) -> bool:
    return is_mailcow(c, settings.mailcow_dir)


def _updatable(c: ContainerInfo) -> tuple[bool, str | None]:
    if not c.project:
        return False, "Not started by compose"
    if not c.working_dir or not os.path.isdir(c.working_dir):
        return False, "Project files not mounted"
    if c.state not in _UPDATABLE_STATES:
        return False, f"Container is {c.state}"
    return True, None


async def _check_image(image_ref: str, registry: RegistryClient) -> _Remote:
    ref = parse_image_ref(image_ref)
    wants_tags = ref.tag is not None and parse_version_tag(ref.tag) is not None
    calls = [registry.head_digest(ref)]
    if wants_tags:
        calls.append(registry.list_tags(ref))
    results = await asyncio.gather(*calls, return_exceptions=True)
    remote = _Remote()
    if isinstance(results[0], BaseException):
        remote.digest_error = str(results[0]) or type(results[0]).__name__
        remote.not_found = isinstance(results[0], NotFoundError)
    else:
        remote.digest = results[0]
    if wants_tags:
        if isinstance(results[1], BaseException):
            remote.tags_error = f"Could not list tags: {results[1] or type(results[1]).__name__}"
        else:
            remote.tags = list(results[1])
    return remote


def _status_for(
    c: ContainerInfo, remote: _Remote | None, rules: Rules,
    allowed_tags: list[str] | None = None, tag_warnings: list[str] | None = None,
) -> ContainerStatus:
    updatable, reason = _updatable(c)
    warnings = [*rules.warnings, *(tag_warnings or [])]

    def make(status: Status, **kw) -> ContainerStatus:
        return ContainerStatus(
            container=c, status=status, updatable=updatable, not_updatable_reason=reason,
            rule_warnings=warnings, notify=rules.notify, **kw
        )

    if not rules.watch:
        return make(Status.NOT_WATCHED, message="Not watched")

    ref = parse_image_ref(c.image_ref)
    if ref.digest:
        return make(Status.NOT_CHECKABLE, message="Pinned by digest")
    if not c.running_repo_digests:
        return make(Status.NOT_CHECKABLE, message="Image built locally")
    assert remote is not None
    if remote.not_found:
        return make(Status.NOT_CHECKABLE, message=remote.digest_error)
    if remote.digest_error is not None:
        return make(Status.ERROR, message=remote.digest_error)
    running = {_sha(d) for d in c.running_repo_digests}
    if remote.digest in running:
        status = Status.UP_TO_DATE
    elif c.local_tag_image_id != c.image_id and remote.digest in {
        _sha(d) for d in c.local_tag_repo_digests
    }:
        status = Status.PULLED_NOT_RECREATED
    else:
        status = Status.NEWER_BUILD
    minor = major = None
    if allowed_tags is not None:
        updates = find_version_updates(ref.tag, allowed_tags)
        minor, major = updates.minor, updates.major
    return make(
        status,
        remote_digest=remote.digest,
        minor_update=minor,
        major_update=major,
        message=remote.tags_error,
    )


def _checkable(c: ContainerInfo) -> bool:
    return bool(c.running_repo_digests) and not parse_image_ref(c.image_ref).digest


async def check_all(
    containers: list[ContainerInfo],
    registry: RegistryClient,
    settings: Settings,
    mailcow_check: Callable[[], Awaitable[MailcowStatus]] | None,
    rules_for: Callable[[ContainerInfo], Rules] | None = None,
) -> CheckResult:
    mailcow_containers = [c for c in containers if _is_mailcow(c, settings)]
    regular = [c for c in containers if not _is_mailcow(c, settings)]

    rules_of = rules_for or (lambda _c: Rules())
    rules = {c.id: rules_of(c) for c in regular}
    # An image is only looked up if some container using it is watched.
    refs = list(dict.fromkeys(c.image_ref for c in regular if _checkable(c) and rules[c.id].watch))
    remotes = dict(
        zip(refs, await asyncio.gather(*(_check_image(r, registry) for r in refs)))
    )

    # Pattern matching is CPU work: do it off the event loop, on its own small pool.
    async def filtered(c: ContainerInfo) -> tuple[list[str] | None, list[str]]:
        remote = remotes.get(c.image_ref)
        if not (rules[c.id].watch and remote is not None and remote.tags is not None):
            return None, []
        tag_filter = TagFilter(rules[c.id])
        allowed = await asyncio.get_running_loop().run_in_executor(
            tag_executor(), tag_filter.filter, remote.tags)
        return allowed, tag_filter.warnings

    tag_results = await asyncio.gather(*(filtered(c) for c in regular))

    mailcow = None
    if mailcow_containers and not os.path.isdir(settings.mailcow_dir):
        # Mailcow runs here but its folder isn't mounted: say so rather than hide it.
        mailcow = MailcowStatus(
            error=f"Mailcow folder not mounted (MAILCOW_DIR={settings.mailcow_dir})"
        )
    elif mailcow_containers and mailcow_check:
        try:
            mailcow = await mailcow_check()
        except Exception as exc:  # noqa: BLE001 – surface any failure as a status
            mailcow = MailcowStatus(error=str(exc) or type(exc).__name__)

    return CheckResult(
        checked_at=datetime.now(timezone.utc),
        containers=[
            _status_for(c, remotes.get(c.image_ref), rules[c.id], *tag_results[i])
            for i, c in enumerate(regular)
        ],
        mailcow=mailcow,
    )
