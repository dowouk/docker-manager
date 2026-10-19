"""Where to read about an update: the owner's own link, the image's GitHub source, or Docker Hub."""
import re
from urllib.parse import quote

from app.imageref import parse_image_ref
from app.inventory import ContainerInfo
from app.rules import Rules, is_plain_http_url

_GITHUB_SOURCE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/?$")


def release_notes_url(info: ContainerInfo, rules: Rules) -> str | None:
    custom = (rules.release_notes or "").strip()
    if custom and is_plain_http_url(custom):
        return custom
    source = (info.image_source or "").strip()
    if _GITHUB_SOURCE.match(source):
        return source.rstrip("/") + "/releases"
    try:
        ref = parse_image_ref(info.image_ref)
    except ValueError:
        return None
    if not info.image_ref.strip() or not ref.is_docker_hub:
        return None
    namespace, _, name = ref.repository.partition("/")
    if not name:
        return None
    if namespace == "library":
        return f"https://hub.docker.com/_/{quote(name, safe='/')}/tags"
    return f"https://hub.docker.com/r/{quote(namespace)}/{quote(name, safe='/')}/tags"
