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
                state=attrs.get("State", {}).get("Status", c.status),
                is_self=bool(self_id)
                and len(self_id) >= _MIN_SELF_ID_LEN
                and c.id.startswith(self_id),
            )
        )
    return result
