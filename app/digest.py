"""Weekly digest email: render the latest check (the notifier sends it)."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import jinja2

from collections.abc import Callable

from app.checker import CheckResult, ContainerStatus, Status
from app.config import Settings
from app.imageref import parse_image_ref
from app.inventory import ContainerInfo
from app.layout import LayoutProblem

_TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
_BUILD = (Status.NEWER_BUILD, Status.PULLED_NOT_RECREATED)
_COULDNT_CHECK = "Couldn't check"
MAX_LAYOUT_LINES = 10
NotesFor = Callable[[ContainerInfo], str | None]


def notifiable_layout(result: CheckResult) -> list[LayoutProblem]:
    """One problem per key (replicas share keys), without containers whose `notify` rule is off.

    Empty when no layout check has run (`layout is None`) – callers that must tell "not checked"
    from "no problems" look at `result.layout` themselves.
    """
    quiet = {cs.container.name for cs in result.containers if not cs.notify}
    seen: set[str] = set()
    out = []
    for p in result.layout or []:
        if p.container in quiet or p.key in seen:
            continue
        seen.add(p.key)
        out.append(p)
    return out


def layout_lines(problems: list[LayoutProblem]) -> list[str]:
    """Up to MAX_LAYOUT_LINES lines, then "and N more"."""
    lines = [f"{p.container}: {p.detail}" for p in problems[:MAX_LAYOUT_LINES]]
    if len(problems) > MAX_LAYOUT_LINES:
        lines.append(f"and {len(problems) - MAX_LAYOUT_LINES} more")
    return lines


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def layout_summary(problems: list[LayoutProblem]) -> str:
    """"2 layout problems and 1 notice": notices (not started by compose) count separately."""
    notices = sum(1 for p in problems if p.severity == "notice")
    found = len(problems) - notices
    parts = ([_plural(found, "layout problem")] if found else []) \
        + ([_plural(notices, "notice")] if notices else [])
    return " and ".join(parts)


def _sha(digest: str | None) -> str:
    if not digest:
        return "unknown"
    return digest.rsplit("@", 1)[-1].removeprefix("sha256:")[:12]


def _current_digest(cs: ContainerStatus) -> str:
    digests = cs.container.running_repo_digests
    return _sha(digests[0]) if digests else "unknown"


def _row(cs: ContainerStatus, change: str, notes: str | None = None) -> dict:
    row = {"name": cs.container.name, "image": cs.container.image_ref, "change": change}
    if notes:
        row["notes"] = notes
    return row


def _sections(result: CheckResult, notes_for: NotesFor | None = None) -> list[dict]:
    mailcow, builds, minors, majors, unchecked = [], [], [], [], []
    mc = result.mailcow
    if mc and mc.error:
        unchecked.append({"name": "Mailcow", "image": "", "change": mc.error})
    elif mc and mc.update_available:
        mailcow.append({
            "name": "Mailcow", "image": "",
            "change": f"{mc.current_version or 'unknown'} → {mc.latest_release or 'unknown'}",
        })
    for cs in result.containers:
        if not cs.notify or cs.status == Status.NOT_WATCHED:
            continue  # unwatched, or kept out by the container's `notify` rule
        tag = parse_image_ref(cs.container.image_ref).tag or "latest"
        notes = notes_for(cs.container) if notes_for else None
        if cs.status in _BUILD:
            suffix = " (pulled, not yet recreated)" if cs.status == Status.PULLED_NOT_RECREATED else ""
            builds.append(_row(cs, f"{_current_digest(cs)} → {_sha(cs.remote_digest)}{suffix}", notes))
        if cs.minor_update:
            minors.append(_row(cs, f"{tag} → {cs.minor_update}", notes))
        if cs.major_update:
            majors.append(_row(cs, f"{tag} → {cs.major_update}", notes))
        if cs.status == Status.NOT_CHECKABLE:
            continue  # a local or pinned image: nothing to report
        if cs.status == Status.ERROR:
            unchecked.append(_row(cs, cs.message or STATUS_TEXT[cs.status]))
        elif cs.message:
            # The digest was checked but the tag listing failed (tags_error).
            unchecked.append(_row(cs, cs.message))
    sections = [
        ("Mailcow", mailcow),
        ("Newer build available", builds),
        ("Minor/patch updates", minors),
        ("Major updates (update by hand)", majors),
        (_COULDNT_CHECK, unchecked),
    ]
    return [{"heading": h, "rows": rows} for h, rows in sections if rows]


STATUS_TEXT = {Status.ERROR: "Error", Status.NOT_CHECKABLE: "Not checkable"}


def _update_count(result: CheckResult) -> int:
    """Updates the dashboard can apply (major versions are update-by-hand, so not counted)."""
    count = sum(1 for cs in result.containers
                if cs.notify and (cs.status in _BUILD or cs.minor_update))
    mc = result.mailcow
    if mc and not mc.error and mc.update_available:
        count += 1
    return count


def render_digest(result: CheckResult, settings: Settings, skipped: int = 0,
                  notes_for: NotesFor | None = None) -> tuple[str, str, str] | None:
    """Return (subject, html, plain), or None when there is nothing to report and it's disabled.

    `result` should already be the visible one (skips applied); `skipped` is how many updates
    that hid. `notes_for` gives a container's release-notes link.
    """
    count = _update_count(result)
    sections = _sections(result, notes_for)
    layout = notifiable_layout(result)
    problems = bool(layout) or any(sec["heading"] == _COULDNT_CHECK for sec in sections)
    if count == 0 and not problems and not settings.digest_when_none:
        return None
    if count == 0 and layout:
        headline = layout_summary(layout)
    elif count == 0:
        headline = "all up to date"
    else:
        headline = f"{count} update{'s' if count != 1 else ''} available"
    subject = f"{settings.server_name}: {headline}"

    local = result.checked_at.astimezone(ZoneInfo(settings.tz))
    ctx = {
        "server_name": settings.server_name,
        "headline": headline,
        "sections": sections,
        "checked_at": f"{local.day} {local:%b %Y, %H:%M}",
        "public_url": settings.public_url,
        "skipped": f"{skipped} update{'s' if skipped != 1 else ''} skipped or snoozed"
        if skipped > 0 else None,
        "layout_count": len(layout),
        "layout_summary": layout_summary(layout),
        "layout_lines": layout_lines(layout),
        "layout_link": settings.public_url.rstrip("/") + "/layout" if settings.public_url else None,
    }

    def env(autoescape: bool) -> jinja2.Environment:
        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(_TEMPLATES),
            autoescape=autoescape,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    html = env(True).get_template("digest.html").render(**ctx)
    plain = env(False).get_template("digest.txt").render(**ctx)
    return subject, html, plain
