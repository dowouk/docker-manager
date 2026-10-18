"""Docker inventory: a read-only view of the containers on this server."""
import os
from dataclasses import dataclass, field

import docker
import docker.errors

_LABEL_PROJECT = "com.docker.compose.project"
_LABEL_WORKING_DIR = "com.docker.compose.project.working_dir"
_LABEL_CONFIG_FILES = "com.docker.compose.project.config_files"
_LABEL_ENV_FILES = "com.docker.compose.project.environment_file"
_LABEL_SERVICE = "com.docker.compose.service"
# Set on the short-lived helper container that updates the dashboard itself.
HELPER_LABEL = "docker-manager.helper"
_MIN_SELF_ID_LEN = 12


_RULE_LABEL_PREFIX = "docker-manager."
_IMAGE_SOURCE_LABEL = "org.opencontainers.image.source"


@dataclass(frozen=True)
class Mount:
    type: str
    source: str
    destination: str
    read_only: bool
    name: str | None = None


@dataclass
class ContainerInfo:
    id: str
    name: str
    image_ref: str
    image_id: str
    running_repo_digests: list[str]
    local_tag_image_id: str | None
    project: str | None
    working_dir: str | None
    config_files: list[str]
    service: str | None
    state: str
    is_self: bool
    local_tag_repo_digests: list[str] = field(default_factory=list)
    env_files: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    image_source: str | None = None


def container_key(info: ContainerInfo) -> str:
    """Identity for rules and skips: project/service for compose containers, else the name."""
    if info.project and info.service:
        return f"{info.project}/{info.service}"
    return info.name


def detect_self_id() -> str | None:
    """Docker sets the hostname to the short container ID by default."""
    return os.environ.get("HOSTNAME") or None


def _get_image(client: docker.DockerClient, ref: str):
    try:
        return client.images.get(ref)
    except docker.errors.APIError:
        # Includes ImageNotFound; treat any lookup failure as "unknown".
        return None


def _paths(label: str) -> list[str]:
    return [p.strip() for p in label.split(",") if p.strip()]


def _mounts(attrs: dict) -> list[Mount]:
    return [
        Mount(
            type=m.get("Type") or "",
            source=m.get("Source") or "",
            destination=m.get("Destination") or "",
            read_only=not m.get("RW", True),
            name=m.get("Name") or None,
        )
        for m in attrs.get("Mounts") or []
    ]


def _image_source(image) -> str | None:
    if image is None:
        return None
    config = image.attrs.get("Config") or {}
    return (config.get("Labels") or {}).get(_IMAGE_SOURCE_LABEL) or None


def list_containers(client: docker.DockerClient, self_id: str | None) -> list[ContainerInfo]:
    result = []
    for c in client.containers.list(all=True):
        attrs = c.attrs
        labels = c.labels or {}
        if HELPER_LABEL in labels:
            continue  # the dashboard's own self-update helper, not one of your containers
        image_ref = attrs["Config"]["Image"]
        image_id = attrs["Image"]
        running = _get_image(client, image_id)
        digests = list(running.attrs.get("RepoDigests") or []) if running else []
        tagged = _get_image(client, image_ref)
        tagged_digests = list(tagged.attrs.get("RepoDigests") or []) if tagged else []
        config_files = _paths(labels.get(_LABEL_CONFIG_FILES, ""))
        result.append(
            ContainerInfo(
                id=c.id,
                name=c.name,
                image_ref=image_ref,
                image_id=image_id,
                running_repo_digests=digests,
                local_tag_image_id=tagged.id if tagged else None,
                local_tag_repo_digests=tagged_digests,
                project=labels.get(_LABEL_PROJECT),
                working_dir=labels.get(_LABEL_WORKING_DIR),
                config_files=config_files,
                env_files=_paths(labels.get(_LABEL_ENV_FILES, "")),
                service=labels.get(_LABEL_SERVICE),
                labels={k: v for k, v in labels.items() if k.startswith(_RULE_LABEL_PREFIX)},
                mounts=_mounts(attrs),
                image_source=_image_source(running),
                state=attrs.get("State", {}).get("Status", c.status),
                is_self=bool(self_id)
                and len(self_id) >= _MIN_SELF_ID_LEN
                and c.id.startswith(self_id),
            )
        )
    return result
