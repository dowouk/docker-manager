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
_SYSTEM_PREFIXES = ("/etc", "/usr", "/run", "/var/lib/docker", "/sys", "/proc", "/boot")
_SYSTEM_HINT = ("System path – if this is intended, add it to the allowed paths "
                "in Settings → Checking.")
_MAILCOW_VOLUME_PREFIX = "mailcowdockerized_"
_ALWAYS_ALLOWED = ("/var/run/docker.sock", "/run/docker.sock", "/etc/localtime", "/etc/timezone")


@dataclass(frozen=True)
class LayoutProblem:
    """One finding. `key` is "<container_key>:<kind>:<source or volume>[:<destination>]" – stable,
    for notification dedupe. Replicas of one service share keys, so a key can cover several
    containers: dedupe by key, don't count by it."""

    key: str
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
                f"Describe it in a compose file under `{posixpath.join(root, c.name)}` and start it from there, "
                "so its files live in one place.", "notice")
            continue

        folder = posixpath.join(root, project)
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
            _check_mount(c, m, folder, root, fine, data_dir, settings, add)
    return found


def _check_mount(c, m: Mount, folder, root, fine, data_dir, settings, add) -> None:
    if c.is_self and _norm(m.destination) == _norm(data_dir):
        return
    # The dashboard's own REGISTRY_AUTH_FILE folder (the README mounts /root/.docker, where
    # `docker login` writes) – moving it would stop new logins reaching the dashboard.
    auth = settings.registry_auth_file
    if c.is_self and auth and _under(_norm(auth), _norm(m.destination)):
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
        system = (not m.source.startswith("/") or src == "/"
                  or any(_under(src, p) for p in _SYSTEM_PREFIXES))
        hint = _SYSTEM_HINT if system else (
            f"Move the data to `{folder}/{last}` and change `{m.source}:{m.destination}` "
            f"to `./{last}:{m.destination}`.")
        add("bind_outside", f"{src}:{m.destination}",
            f"Bind mount outside {root}: {m.source} to {m.destination}", hint)
    elif m.type == "volume":
        name = m.name or ""
        if (settings.named_volumes_allowed or not name or _ANONYMOUS_VOLUME.fullmatch(name)
                or name.startswith(_MAILCOW_VOLUME_PREFIX)):
            return
        add("named_volume", f"{name}:{m.destination}", f"Named volume {name} (stored in Docker's own folder) at {m.destination}",
            f"Move the data to `{folder}/data` and change `{name}:{m.destination}` "
            f"to `./data:{m.destination}`. If the volume is bound to a folder under {root} "
            "(driver_opts), allow it with the layout.ignore label or the allowed paths in Settings.")
