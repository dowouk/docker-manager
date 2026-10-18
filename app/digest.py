"""Weekly digest email: render the latest check and send it via Azure Communication Services."""
import os
from html import escape as html_escape
from datetime import datetime
from zoneinfo import ZoneInfo

import jinja2

from app.checker import CheckResult, ContainerStatus, Status
from app.config import Settings
from app.imageref import parse_image_ref

_TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
_BUILD = (Status.NEWER_BUILD, Status.PULLED_NOT_RECREATED)
_COULDNT_CHECK = "Couldn't check"


def _sha(digest: str | None) -> str:
    if not digest:
        return "unknown"
    return digest.rsplit("@", 1)[-1].removeprefix("sha256:")[:12]


def _current_digest(cs: ContainerStatus) -> str:
    digests = cs.container.running_repo_digests
    return _sha(digests[0]) if digests else "unknown"


def _row(cs: ContainerStatus, change: str) -> dict:
    return {"name": cs.container.name, "image": cs.container.image_ref, "change": change}


def _sections(result: CheckResult) -> list[dict]:
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
        tag = parse_image_ref(cs.container.image_ref).tag or "latest"
        if cs.status in _BUILD:
            suffix = " (pulled, not yet recreated)" if cs.status == Status.PULLED_NOT_RECREATED else ""
            builds.append(_row(cs, f"{_current_digest(cs)} → {_sha(cs.remote_digest)}{suffix}"))
        if cs.minor_update:
            minors.append(_row(cs, f"{tag} → {cs.minor_update}"))
        if cs.major_update:
            majors.append(_row(cs, f"{tag} → {cs.major_update}"))
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
    count = sum(1 for cs in result.containers if cs.status in _BUILD or cs.minor_update)
    mc = result.mailcow
    if mc and not mc.error and mc.update_available:
        count += 1
    return count


def render_digest(result: CheckResult, settings: Settings) -> tuple[str, str, str] | None:
    """Return (subject, html, plain), or None when there is nothing to report and it's disabled."""
    count = _update_count(result)
    sections = _sections(result)
    problems = any(sec["heading"] == _COULDNT_CHECK for sec in sections)
    if count == 0 and not problems and not settings.digest_when_none:
        return None
    if count == 0:
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


def _default_factory(connection_string: str):
    from azure.communication.email import EmailClient

    return EmailClient.from_connection_string(connection_string)


def _send(settings: Settings, subject: str, html: str, plain: str, factory) -> None:
    client = (factory or _default_factory)(settings.acs_connection_string)
    message = {
        "senderAddress": settings.digest_from,
        "recipients": {"to": [{"address": a} for a in settings.digest_to]},
        "content": {"subject": subject, "html": html, "plainText": plain},
    }
    client.begin_send(message).result()


def send_digest(settings: Settings, result: CheckResult, email_client_factory=None) -> str:
    """Send the digest. Blocking – call via asyncio.to_thread from async code."""
    if not settings.digest_enabled:
        return "skipped – email not configured"
    rendered = render_digest(result, settings)
    if rendered is None:
        return "skipped – nothing to report"
    _send(settings, *rendered, email_client_factory)
    return "sent"


class EmailNotConfigured(ValueError):
    """Email settings are incomplete."""


def send_test_email(settings: Settings, email_client_factory=None) -> None:
    """Send a short message to the digest recipients. Blocking, like send_digest."""
    if not settings.digest_enabled:
        raise EmailNotConfigured("Email is not configured")
    name = settings.server_name
    plain = (f"This is a test email from Docker Manager on {name}. "
             "If you can read this, email is working.")
    html = f"<p>This is a test email from Docker Manager on {html_escape(name)}. " \
           "If you can read this, email is working.</p>"
    _send(settings, f"{name}: test email", html, plain, email_client_factory)
