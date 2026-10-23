"""Edit a stack's compose files and env file from the dashboard.

The files are always worked out from the container's compose labels, never from the
form, and only files inside COMPOSE_ROOT can be changed. Every change is checked
with `docker compose config` against copies before anything is written, and the
old file is backed up first.
"""
import asyncio
import contextlib
import difflib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime

from app.compose_edit import _atomic_write, _backup
from app.inventory import ContainerInfo
from app.jobs import subprocess_env

MAX_BYTES = 1024 * 1024
BACKUPS_SHOWN = 20
CHECK_TIMEOUT = 60.0
CHANGED_MESSAGE = "A file changed since you opened it – reload the page and make your edits again"


class ConfigError(Exception):
    pass


@dataclass
class ConfigFile:
    path: str
    kind: str  # "compose" or "env"
    text: str
    sha256: str  # of the file on disk; "" when it doesn't exist yet
    exists: bool
    editable: bool
    reason: str | None = None  # why it can't be edited (or read)
    crlf: bool = False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def env_paths(info: ContainerInfo) -> list[str]:
    """The env files compose read: those it recorded, else the project folder's .env."""
    if info.env_files:
        return list(info.env_files)
    return [os.path.join(info.working_dir, ".env")] if info.working_dir else []


def _inside(path: str, root: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([real, base]) == base and real != base


def _load(path: str, kind: str, compose_root: str) -> ConfigFile:
    if not _inside(path, compose_root):
        editable, reason = False, f"Outside {compose_root} – edit it on the server"
    else:
        editable, reason = True, None
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        if kind == "compose":
            return ConfigFile(path, kind, "", "", False, False, "The file was not found")
        # A missing env file is created on save if anything is typed in.
        return ConfigFile(path, kind, "", "", False, editable, reason)
    except OSError as exc:
        return ConfigFile(path, kind, "", "", True, False,
                          f"The file could not be read ({exc.strerror or exc})")
    if len(data) > MAX_BYTES:
        return ConfigFile(path, kind, "", "", True, False, "Over 1 MB – edit it on the server")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ConfigFile(path, kind, "", _sha(data), True, False,
                          "Not UTF-8 text – edit it on the server")
    return ConfigFile(path, kind, text.replace("\r\n", "\n"), _sha(data), True, editable,
                      reason, crlf="\r\n" in text)


def stack_files(info: ContainerInfo, compose_root: str) -> list[ConfigFile]:
    return ([_load(p, "compose", compose_root) for p in info.config_files]
            + [_load(p, "env", compose_root) for p in env_paths(info)])


def normalise(text: str) -> str:
    """Browsers send CRLF; keep the file's own line endings and a final newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _encode(text: str, f: ConfigFile) -> bytes:
    return (text.replace("\n", "\r\n") if f.crlf else text).encode("utf-8")


def changed(files: list[ConfigFile], texts: list[str]) -> list[int]:
    return [i for i, (f, t) in enumerate(zip(files, texts)) if f.editable and t != f.text]


def check_unchanged(files: list[ConfigFile], shas: list[str]) -> None:
    """Refuse to save over a file someone changed after the page was loaded."""
    for f, sha in zip(files, shas, strict=True):
        if f.sha256 != sha:
            raise ConfigError(CHANGED_MESSAGE)


async def validate(info: ContainerInfo, files: list[ConfigFile], texts: list[str]) -> None:
    """Run `docker compose config` on copies of the edited files. Raises ConfigError."""
    with tempfile.TemporaryDirectory(prefix="dm-config-") as tmp:
        cmd = ["docker", "compose", "-p", info.project, "--project-directory", info.working_dir]
        for i, (f, text) in enumerate(zip(files, texts)):
            if f.kind == "env" and not f.exists and not text:
                continue  # compose looks for it and finds nothing, as it does now
            if not f.editable:
                cmd += ["-f" if f.kind == "compose" else "--env-file", f.path]  # as it is
                continue
            copy = os.path.join(tmp, f"{i}-{os.path.basename(f.path)}")
            fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(text.encode("utf-8"))
            cmd += ["-f" if f.kind == "compose" else "--env-file", copy]
        cmd += ["config", "--quiet"]
        env = subprocess_env()
        env["PWD"] = info.working_dir  # as a shell would: compose files use ${PWD}
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=info.working_dir, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise ConfigError(f"The check could not run ({exc.strerror or exc})") from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise ConfigError("The check timed out – nothing was saved") from None
        if proc.returncode != 0:
            # Show the copies under their real names.
            message = out.decode(errors="replace").strip()
            for i, f in enumerate(files):
                message = message.replace(os.path.join(tmp, f"{i}-{os.path.basename(f.path)}"), f.path)
            raise ConfigError(message or f"docker compose config failed (exit code {proc.returncode})")


def _create(path: str, data: bytes) -> None:
    """A new env file: private to its owner, owned like the folder it's in."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".compose-edit-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        st = os.stat(folder)
        with contextlib.suppress(OSError):
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _env_names(text: str) -> dict[str, str]:
    names = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        names[key] = line.split("=", 1)[1]
    return names


def env_summary(old: str, new: str) -> str:
    """Which variables changed – names only, never values."""
    a, b = _env_names(old), _env_names(new)
    parts = []
    for word, names in (("added", sorted(b.keys() - a.keys())),
                        ("changed", sorted(k for k in a.keys() & b.keys() if a[k] != b[k])),
                        ("removed", sorted(a.keys() - b.keys()))):
        if names:
            parts.append(f"{word} {', '.join(names)}")
    return "; ".join(parts) or "comments or layout only"


def compose_diff(f: ConfigFile, new: str) -> str:
    return "\n".join(difflib.unified_diff(
        f.text.splitlines(), new.splitlines(), fromfile=f.path, tofile=f.path, lineterm=""))


def save(files: list[ConfigFile], texts: list[str], backup_dir: str, project: str,
         now: datetime) -> tuple[str, str]:
    """Back up and write every changed file. Returns (one-line summary, compose diff)."""
    todo = changed(files, texts)
    originals = {}
    for i in todo:  # check every file before writing any
        f = files[i]
        if f.exists:
            with open(f.path, "rb") as cur:
                originals[i] = cur.read()
            if _sha(originals[i]) != f.sha256:
                raise ConfigError(CHANGED_MESSAGE)
        elif os.path.lexists(f.path):
            raise ConfigError(CHANGED_MESSAGE)
    lines, diffs = [], []
    for i in todo:
        f, text = files[i], texts[i]
        data = _encode(text, f)
        if f.exists:
            _backup(f.path, originals[i], backup_dir, project, now)
            _atomic_write(f.path, data)
        else:
            _create(f.path, data)
        if f.kind == "env":
            lines.append(f"{f.path}: {env_summary(f.text, text)}")
        else:
            lines.append(f.path)
            diffs.append(compose_diff(f, text))
    return f"Edited the config of {project} – " + "; ".join(lines), "\n".join(diffs)


# --- backups: <backup_dir>/<project>/<file name>.<YYYYmmdd-HHMMSS>[.n] -----------------

_BACKUP_RE = re.compile(r"^(?P<name>.+)\.(?P<stamp>\d{8}-\d{6})(?:\.(?P<n>\d+))?$")
STAMP_RE = re.compile(r"^\d{8}-\d{6}$")


@dataclass
class Backup:
    stamp: str  # when it was taken, in the dashboard's time zone
    at: datetime
    paths: dict[int, str] = field(default_factory=dict)  # stack file index -> backup file

    @property
    def label(self) -> str:
        return f"{self.at.day} {self.at:%b %Y, %H:%M:%S}"


def _by_name(files: list[ConfigFile]) -> dict[str, int]:
    """Backups are named after the file alone: only names used once in the stack map back."""
    counts: dict[str, list[int]] = {}
    for i, f in enumerate(files):
        counts.setdefault(os.path.basename(f.path), []).append(i)
    return {name: idx[0] for name, idx in counts.items() if len(idx) == 1 and files[idx[0]].editable}


def list_backups(files: list[ConfigFile], backup_dir: str, project: str,
                 limit: int | None = BACKUPS_SHOWN) -> list[Backup]:
    """The stack's backups, newest first; files saved in the same second form one backup."""
    folder = os.path.join(backup_dir, project)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    index = _by_name(files)
    found: dict[str, Backup] = {}
    best: dict[tuple[str, int], int] = {}
    for name in names:
        m = _BACKUP_RE.match(name)
        if not m or m["name"] not in index:
            continue
        try:
            at = datetime.strptime(m["stamp"], "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        i, n = index[m["name"]], int(m["n"] or 0)
        if best.get((m["stamp"], i), -1) >= n:
            continue  # the same file backed up twice in one second: the later copy wins
        best[(m["stamp"], i)] = n
        found.setdefault(m["stamp"], Backup(m["stamp"], at)).paths[i] = os.path.join(folder, name)
    backups = sorted(found.values(), key=lambda b: b.stamp, reverse=True)
    return backups[:limit] if limit else backups


def load_backup(files: list[ConfigFile], backup_dir: str, project: str,
                stamp: str) -> tuple[Backup, list[str]]:
    """The editor texts with that backup's files put in place of the current ones."""
    backup = next((b for b in list_backups(files, backup_dir, project, limit=None)
                   if b.stamp == stamp), None)
    if backup is None:
        raise ConfigError("That backup was not found")
    texts = [f.text for f in files]
    for i, path in backup.paths.items():
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ConfigError(f"The backup of {files[i].path} is over 1 MB – restore it on the server")
            texts[i] = normalise(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"The backup of {files[i].path} could not be read ({exc})") from exc
    return backup, texts
