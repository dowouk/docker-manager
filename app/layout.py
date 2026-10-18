"""Layout check: flag anything a container keeps outside COMPOSE_ROOT. Read-only and pure."""
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.config import Settings
from app.inventory import ContainerInfo, Mount, container_key, is_mailcow
from app.rules import Rules

Kind = Literal["bind_outside", "named_volume", "compose_outside", "env_outside", "not_compose"]
Severity = Literal["problem", "notice"]

_ANONYMOUS_VOLUME = re.compile(r"[0-9a-f]{64}")
_ALWAYS_ALLOWED = ("/var/run/docker.sock", "/run/docker.sock", "/etc/localtime", "/etc/timezone")


@dataclass(frozen=True)
class LayoutProblem:
    key: str  # "<container_key>:<kind>:<path or volume>" – stable, for notification dedupe
    container: str
    project: str | None
    kind: Kind
    detail: str
    hint: str
    severity: Severity


def _norm(path: str) -> str:
    """Normalise without touching the filesystem (no symlink resolution)."""
    return posixpath.normpath("/" + path.lstrip("/")) if path.startswith("/") else posixpath.normpath(path)


def _under(path: str, root: str) -> bool:
    """Whether `path` is `root` or inside it, by path components (`/dockerx` is not under `/docker`)."""
    path, root = _norm(path), _norm(root)
    if root == "/":
        return path.startswith("/")
    return path == root or path.startswith(root + "/")


def _last_part(path: str) -> str:
    return posixpath.basename(_norm(path)) or "data"


def layout_problems(
    containers: list[ContainerInfo],
    settings: Settings,
    rules_for: Callable[[ContainerInfo], Rules],
    data_dir: str,
    mailcow_dir: str,
) -> list[LayoutProblem]:
    if not settings.layout_check:
        return []
    root = _norm(settings.compose_root)
    allow = [_norm(p) for p in settings.layout_allowlist if p.startswith("/")]

    def fine(path: str) -> bool:
        return _under(path, root) or _under(path, mailcow_dir) or any(_under(path, a) for a in allow)

    found: list[LayoutProblem] = []
    for c in containers:
        if is_mailcow(c, mailcow_dir) or rules_for(c).layout_ignore:
            continue
        ckey = container_key(c)
        project = c.project

        def add(kind: Kind, what: str, detail: str, hint: str, severity: Severity = "problem") -> None:
            found.append(LayoutProblem(f"{ckey}:{kind}:{what}", c.name, project, kind, detail, hint, severity))

        if not project:
            add("not_compose", "-", "Not started by compose",
                f"Describe it in a compose file under `{root}/{c.name}` and start it from there, "
                "so its files live in one place.", "notice")
            continue

        folder = f"{root}/{project}"
        compose_paths = c.config_files or ([c.working_dir] if c.working_dir else [])
        for path in compose_paths:
            if not fine(path):
                add("compose_outside", _norm(path), f"Compose file outside {root}: {path}",
                    f"Move the project folder to `{folder}` and start it from there.")
        for path in c.env_files:
            if not fine(path):
                add("env_outside", _norm(path), f"Env file outside {root}: {path}",
                    f"Move the env file to `{folder}/.env` and point the project at it.")
        for m in c.mounts:
            _check_mount(c, m, folder, fine, data_dir, settings, add)
    return found


def _check_mount(c, m: Mount, folder, fine, data_dir, settings, add) -> None:
    if c.is_self and m.destination == data_dir:
        return
    if m.type == "bind":
        src = _norm(m.source)
        if src in _ALWAYS_ALLOWED or _under(src, "/dev") or _under(src, "/proc"):
            return
        if _under(src, "/sys") and m.read_only:
            return
        if fine(src):
            return
        last = _last_part(src)
        add("bind_outside", src, f"Bind mount outside {posixpath.dirname(folder)}: {m.source} to {m.destination}",
            f"Move the data to `{folder}/{last}` and change `{m.source}:{m.destination}` "
            f"to `./{last}:{m.destination}`.")
    elif m.type == "volume":
        name = m.name or ""
        if settings.named_volumes_allowed or not name or _ANONYMOUS_VOLUME.fullmatch(name):
            return
        add("named_volume", name, f"Named volume {name} (stored in Docker's own folder) at {m.destination}",
            f"Move the data to `{folder}/data` and change `{name}:{m.destination}` "
            f"to `./data:{m.destination}`.")
