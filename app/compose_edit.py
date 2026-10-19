"""Targeted text edits of compose files: the image line of a service, and the options
the dashboard's own service needs for Mailcow.

Deliberately a line scanner, never a YAML load/dump, so that only the lines that must
change do, and everything else (comments, quoting, line endings) stays identical.
"""

import difflib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime

_HAND = " – update by hand"
_NOT_FOUND = "Image line not found – it may come from an include or extends file"
_ENC = {"encoding": "utf-8", "errors": "surrogateescape"}


class EditRefused(Exception):
    """Raised with a user-facing message that ends with '– update by hand'."""


def _refuse(reason: str) -> EditRefused:
    return EditRefused(reason + _HAND)


@dataclass
class TagChange:
    path: str
    line_no: int  # 1-based
    old_line: str  # without line ending
    new_line: str  # without line ending
    diff: str
    file_sha256: str  # hash of the whole file at plan time


def _split_lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _body(line: str) -> str:
    return line.rstrip("\n").removesuffix("\r") if line.endswith("\n") else line


def _ending(line: str) -> str:
    return line[len(_body(line)):]


def _is_blank_or_comment(body: str) -> bool:
    s = body.strip()
    return not s or s.startswith("#")


def _indent(body: str) -> int:
    return len(body) - len(body.lstrip(" \t"))


def _find_services(lines: list[str]) -> int | None:
    """Index of the line after top-level `services:`, or None."""
    top = re.compile(r"""^(["']?)services\1\s*:(.*)$""")
    for i, raw in enumerate(lines):
        m = top.match(_body(raw))
        if not m:
            continue
        rest = m.group(2).strip()
        if rest and not rest.startswith("#"):
            raise _refuse("The services section is written in flow style")
        return i + 1
    for raw in lines:
        if re.match(r"""^\s+(["']?)services\1\s*:""", _body(raw)):
            raise _refuse("The services key is not at the top level of the file")
    return None


def _parse_value(rest: str) -> tuple[int, int]:
    """Return (start, end) of the image value within `rest` (text after the colon)."""
    start = len(rest) - len(rest.lstrip(" \t"))
    v = rest[start:]
    if not v or v.startswith("#"):
        raise _refuse("The image value is on a following line")
    if v[0] in "|>":
        raise _refuse("The image value is a block scalar")
    if v[0] in "*&!":
        raise _refuse("The image value uses a YAML anchor, alias or tag")
    if v[0] in "\"'":
        q = v[0]
        i = 1
        while i < len(v):
            if q == '"' and v[i] == "\\":
                i += 2
                continue
            if v[i] == q:
                if q == "'" and v[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                break
            i += 1
        else:
            raise _refuse("The image value runs over several lines")
        tail = v[i + 1 :].strip()
        if tail and not tail.startswith("#"):
            raise _refuse("The image line has unexpected text after the value")
        return start + 1, start + i
    m = re.search(r"\s#", v)
    end = m.start() if m else len(v)
    return start, start + len(v[:end].rstrip())


def _scan_file(path: str, lines: list[str], service: str) -> tuple[int, int, int] | None:
    """Locate the image value for `service`: (line index, value start, value end)."""
    i = _find_services(lines)
    if i is None:
        return None
    svc_re = re.compile(r"""^( *)(["']?)%s\2\s*:(.*)$""" % re.escape(service))
    child_indent = None
    svc_at = None
    n = len(lines)
    while i < n:
        body = _body(lines[i])
        if _is_blank_or_comment(body):
            i += 1
            continue
        ind = _indent(body)
        if ind == 0:
            break  # left the services mapping
        if "\t" in body[:ind]:
            raise _refuse("The compose file indents with tabs")
        if child_indent is None:
            child_indent = ind
        m = svc_re.match(body) if ind == child_indent else None
        if m:
            rest = m.group(3).strip()
            if rest.startswith("{"):
                raise _refuse(f"Service '{service}' is written in flow style")
            if rest and not rest.startswith("#"):
                raise _refuse(f"Service '{service}' is not a plain block mapping")
            svc_at = i
            break
        i += 1
    if svc_at is None:
        return None

    svc_indent = _indent(_body(lines[svc_at]))
    img_re = re.compile(r"""^( *)(["']?)image\2\s*:(.*)$""")
    block_indent = None
    j = svc_at + 1
    while j < n:
        body = _body(lines[j])
        if _is_blank_or_comment(body):
            j += 1
            continue
        ind = _indent(body)
        if ind <= svc_indent:
            break
        if "\t" in body[:ind]:
            raise _refuse("The compose file indents with tabs")
        if block_indent is None:
            block_indent = ind
        m = img_re.match(body) if ind == block_indent else None
        if m:
            rest = m.group(3)
            colon = len(body) - len(rest)
            s, e = _parse_value(rest)
            value = rest[s:e]
            if "${" in value or re.search(r"\$[A-Za-z_]", value):
                raise _refuse("Version set by variable")
            for k in range(j + 1, n):
                nxt = _body(lines[k])
                if _is_blank_or_comment(nxt):
                    continue
                if _indent(nxt) > ind:
                    raise _refuse("The image value runs over several lines")
                break
            return j, colon + s, colon + e
        j += 1
    return None


_IMAGE_RE = re.compile(r"[A-Za-z0-9._/:@+-]+")


def plan_tag_change(config_files: list[str], service: str, new_image: str) -> TagChange:
    if not _IMAGE_RE.fullmatch(new_image):
        raise _refuse("The new image reference contains unexpected characters")
    for path in reversed(config_files):
        with open(path, "rb") as f:
            raw_bytes = f.read()
        text = raw_bytes.decode(**_ENC)
        lines = _split_lines(text)
        found = _scan_file(path, lines, service)
        if found is None:
            continue
        idx, s, e = found
        old_body = _body(lines[idx])
        new_body = old_body[:s] + new_image + old_body[e:]
        new_lines = list(lines)
        new_lines[idx] = new_body + _ending(lines[idx])
        diff = "\n".join(
            difflib.unified_diff(
                [_body(x) for x in lines],
                [_body(x) for x in new_lines],
                fromfile=path,
                tofile=path,
                lineterm="",
            )
        )
        return TagChange(path, idx + 1, old_body, new_body, diff, hashlib.sha256(raw_bytes).hexdigest())
    raise _refuse(_NOT_FOUND)


def _atomic_write(path: str, data: bytes) -> None:
    target = os.path.realpath(path)
    st = os.stat(target)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".compose-edit-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup(path: str, original: bytes, backup_dir: str, project: str, now: datetime) -> str:
    """Keep a copy of the file as it was (never overwriting an earlier backup)."""
    backup_path = os.path.join(
        backup_dir, project, os.path.basename(path) + "." + now.strftime("%Y%m%d-%H%M%S")
    )
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    mode = os.stat(path).st_mode & 0o7777
    base, n = backup_path, 0
    while True:
        try:
            fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
            backup_path = f"{base}.{n}"
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), mode)
        f.write(original)
        f.flush()
        os.fsync(f.fileno())
    return backup_path


def apply_tag_change(change: TagChange, backup_dir: str, project: str, now: datetime) -> str:
    with open(change.path, "rb") as f:
        original = f.read()
    lines = _split_lines(original.decode(**_ENC))
    idx = change.line_no - 1
    if (
        hashlib.sha256(original).hexdigest() != change.file_sha256
        or idx >= len(lines)
        or _body(lines[idx]) != change.old_line
    ):
        raise EditRefused("Compose file changed since preview")

    backup_path = _backup(change.path, original, backup_dir, project, now)
    lines[idx] = change.new_line + _ending(lines[idx])
    _atomic_write(change.path, "".join(lines).encode(**_ENC))
    return backup_path


def restore_backup(change: TagChange, backup_path: str) -> None:
    with open(backup_path, "rb") as f:
        _atomic_write(change.path, f.read())


# --- the dashboard's own service: options Mailcow needs -----------------------

# What the dashboard needs to run Mailcow's update.sh on the server (see app/mailcow.py).
MAILCOW_OPTIONS = (("pid", "host"), ("privileged", "true"))
_KEY_RE = re.compile(r"""^( *)(["']?)([A-Za-z_][\w-]*)\2\s*:(.*)$""")


@dataclass
class OptionsChange:
    path: str
    new_text: str
    diff: str
    file_sha256: str  # hash of the whole file at plan time


def _service_block(lines: list[str], service: str) -> tuple[int, int, int] | None:
    """(service line index, end index, block indent) for `service`, or None."""
    i = _find_services(lines)
    if i is None:
        return None
    svc_re = re.compile(r"""^( *)(["']?)%s\2\s*:(.*)$""" % re.escape(service))
    child_indent = None
    n = len(lines)
    while i < n:
        body = _body(lines[i])
        if _is_blank_or_comment(body):
            i += 1
            continue
        ind = _indent(body)
        if ind == 0:
            return None
        if "\t" in body[:ind]:
            raise _refuse("The compose file indents with tabs")
        if child_indent is None:
            child_indent = ind
        m = svc_re.match(body) if ind == child_indent else None
        if m:
            rest = m.group(3).strip()
            if rest and not rest.startswith("#"):
                raise _refuse(f"Service '{service}' is not a plain block mapping")
            break
        i += 1
    else:
        return None
    svc_at, svc_indent = i, _indent(_body(lines[i]))
    block_indent = None
    end = svc_at + 1
    j = svc_at + 1
    while j < n:
        body = _body(lines[j])
        if _is_blank_or_comment(body):
            # An indented comment still belongs to the service (e.g. a last "# [Mailcow]" line).
            if body.strip() and _indent(body) > svc_indent:
                end = j + 1
            j += 1
            continue
        ind = _indent(body)
        if ind <= svc_indent:
            break
        if "\t" in body[:ind]:
            raise _refuse("The compose file indents with tabs")
        if block_indent is None:
            block_indent = ind
        end = j + 1
        j += 1
    if block_indent is None:
        raise _refuse(f"Service '{service}' has no settings")
    return svc_at, end, block_indent


def _plain(value: str) -> str:
    v = value.split(" #")[0].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.lower()


def plan_service_options(config_files: list[str], service: str,
                         options=MAILCOW_OPTIONS) -> OptionsChange | None:
    """The edit that gives `service` these options, or None if it already has them.

    A commented-out "# [Mailcow] pid: host" line in the service is uncommented in place;
    anything still missing is added straight after the service's name. An option that is
    already set to something else is never overwritten."""
    for path in reversed(config_files):
        with open(path, "rb") as f:
            raw_bytes = f.read()
        lines = _split_lines(raw_bytes.decode(**_ENC))
        found = _service_block(lines, service)
        if found is None:
            continue
        svc_at, end, indent = found
        new_lines = list(lines)
        missing = []
        for key, value in options:
            current = None
            for k in range(svc_at + 1, end):
                m = _KEY_RE.match(_body(lines[k]))
                if m and len(m.group(1)) == indent and m.group(3) == key:
                    current = _plain(m.group(4))
                    break
            if current == value:
                continue
            if current is not None:
                raise _refuse(f"The dashboard's service already sets {key}: {current}")
            commented = re.compile(r"^\s*#\s*\[Mailcow\]\s*%s\s*:\s*[\"']?%s[\"']?\s*$"
                                   % (re.escape(key), re.escape(value)), re.IGNORECASE)
            k = next((k for k in range(svc_at + 1, end) if commented.match(_body(lines[k]))), None)
            if k is not None:
                new_lines[k] = " " * indent + f"{key}: {value}" + _ending(lines[k])
            else:
                missing.append(f"{key}: {value}")
        if missing:
            ending = _ending(lines[svc_at]) or "\n"
            added = [" " * indent + line + ending for line in missing]
            new_lines[svc_at + 1:svc_at + 1] = added
        if new_lines == lines:
            return None
        diff = "\n".join(difflib.unified_diff(
            [_body(x) for x in lines], [_body(x) for x in new_lines],
            fromfile=path, tofile=path, lineterm=""))
        return OptionsChange(path, "".join(new_lines), diff,
                             hashlib.sha256(raw_bytes).hexdigest())
    raise _refuse(f"Service '{service}' not found in its compose file")


def apply_options_change(change: OptionsChange, backup_dir: str, project: str,
                         now: datetime) -> str:
    with open(change.path, "rb") as f:
        original = f.read()
    if hashlib.sha256(original).hexdigest() != change.file_sha256:
        raise EditRefused("Compose file changed since preview")
    backup_path = _backup(change.path, original, backup_dir, project, now)
    _atomic_write(change.path, change.new_text.encode(**_ENC))
    return backup_path
