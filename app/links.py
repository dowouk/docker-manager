"""Where to read about an update: the owner's own link, the image's GitHub source, or Docker Hub."""
import re
from urllib.parse import quote, urlsplit

from app.imageref import parse_image_ref
from app.inventory import ContainerInfo
from app.rules import Rules

_GITHUB_SOURCE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/?$")
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")


def _safe_url(url: str | None) -> str | None:
    """The URL if it is plain http(s) with a host, else None."""
    if not url:
        return None
    url = url.strip()
    if _UNSAFE.search(url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return url


def release_notes_url(info: ContainerInfo, rules: Rules) -> str | None:
    custom = _safe_url(rules.release_notes)
    if custom:
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
