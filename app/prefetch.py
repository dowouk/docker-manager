"""Work out which updates can be downloaded ahead of time."""
from dataclasses import dataclass
from typing import Callable, Literal

from app.checker import CheckResult, ContainerStatus, Status
from app.compose_edit import EditRefused, plan_tag_change
from app.imageref import parse_image_ref
from app.inventory import ContainerInfo


@dataclass(frozen=True)
class Target:
    key: str  # state key: image_ref for a build, the new ref ("postgres:16.4") for a version
    kind: Literal["build", "version"]
    info: ContainerInfo  # one container to run the pull for
    ref: str  # what to pull: image_ref (build) or repo:new tag (version)
    remote_digest: str | None  # build only: the digest the check found


def compose_editable(cs: ContainerStatus, tag: str) -> bool:
    """Whether the compose file sets the image as a plain tag we can change to `tag`."""
    info = cs.container
    new_ref = parse_image_ref(info.image_ref).with_tag(tag)
    try:
        plan_tag_change(info.config_files, info.service, new_ref)
    except (EditRefused, OSError):
        return False
    return True


def _build_shared_ok(image_ref: str, containers: list[ContainerInfo], by_id: dict[str, ContainerStatus]) -> bool:
    """Every container using this tag must be watched and waiting for the same newer build."""
    for c in containers:
        if c.image_ref != image_ref:
            continue
        cs = by_id.get(c.id)
        if cs is None or cs.status == Status.NOT_WATCHED or cs.status != Status.NEWER_BUILD:
            return False
    return True


def targets(
    visible: CheckResult,
    containers: list[ContainerInfo],
    editable: Callable[[ContainerStatus, str], bool],
) -> list[Target]:
    by_id = {cs.container.id: cs for cs in visible.containers}
    found: dict[str, Target] = {}
    for cs in visible.containers:
        info = cs.container
        if info.is_self or not cs.updatable or not info.service:
            continue
        if cs.status == Status.NEWER_BUILD:
            key = info.image_ref
            if key not in found and _build_shared_ok(key, containers, by_id):
                found[key] = Target(key, "build", info, info.image_ref, cs.remote_digest)
        if cs.minor_update:
            new_ref = parse_image_ref(info.image_ref).with_tag(cs.minor_update)
            if new_ref not in found and editable(cs, cs.minor_update):
                found[new_ref] = Target(new_ref, "version", info, new_ref, None)
    return list(found.values())
