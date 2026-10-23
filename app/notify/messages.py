"""Notification message bodies: one function per event, each returning Rendered(subject, plain, html).

Recipients are added by the notifier. Job messages never carry log text (logs can contain
environment values), only the job's outcome and a link to it.
"""
import os
from dataclasses import dataclass
from typing import Literal, NamedTuple

import jinja2

from app.checker import CheckResult
from app.config import Settings
from app.digest import render_digest

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")


class Rendered(NamedTuple):
    subject: str
    plain: str
    html: str


@dataclass(frozen=True)
class UpdateItem:
    key: str  # container name + kind, used to avoid re-notifying the same update
    name: str
    image: str
    change: str
    release_notes: str | None = None


def _env(autoescape: bool) -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES),
        autoescape=autoescape,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _one_line(text: str) -> str:
    """Subjects are one line: a server name with a newline must not split the header."""
    return " ".join(text.split())


def _link(settings: Settings, path: str = "") -> str | None:
    if not settings.public_url:
        return None
    return settings.public_url.rstrip("/") + path


def _render(template: str, subject: str, settings: Settings, link: str | None = None,
            link_text: str = "Open the dashboard", **ctx) -> Rendered:
    ctx = {"server_name": settings.server_name, "link": link, "link_text": link_text, **ctx}
    html = _env(True).get_template(f"notify/{template}.html").render(**ctx)
    plain = _env(False).get_template(f"notify/{template}.txt").render(**ctx)
    return Rendered(_one_line(subject), plain, html)


def digest_message(result: CheckResult, settings: Settings, skipped: int = 0,
                   notes_for=None) -> Rendered | None:
    rendered = render_digest(result, settings, skipped, notes_for)
    if rendered is None:
        return None
    subject, html, plain = rendered
    return Rendered(_one_line(subject), plain, html)


def updates_found_message(updates: list[UpdateItem], settings: Settings) -> Rendered:
    n = len(updates)
    headline = f"{n} new update{'s' if n != 1 else ''} available"
    return _render("updates_found", f"{settings.server_name}: {headline}", settings,
                   _link(settings), updates=updates, headline=headline)


def job_message(kind_label: str, target: str, status: Literal["succeeded", "failed"],
                user: str, job_id: int, settings: Settings) -> Rendered:
    word = "failed" if status == "failed" else "finished"
    # Restart / Stop / Start / Apply config name themselves; every other job is an update.
    noun = kind_label.lower() if kind_label in ("Restart", "Stop", "Start", "Apply config") else "update"
    headline = f"{noun} {word} – {target}"
    return _render("job", f"{settings.server_name}: {headline}", settings,
                   _link(settings, f"/jobs/{job_id}"), "Open the job",
                   headline=headline, kind_label=kind_label, target=target,
                   status=status, user=user)


def check_failing_message(failures: int, settings: Settings) -> Rendered:
    return _render("check_failing", f"{settings.server_name}: update check failing",
                   settings, _link(settings), failures=failures)


def check_recovered_message(settings: Settings) -> Rendered:
    return _render("check_recovered", f"{settings.server_name}: update check recovered",
                   settings, _link(settings))


def layout_message(problems: list[str], settings: Settings, summary: str | None = None) -> Rendered:
    return _render("layout_problems", f"{settings.server_name}: layout problems",
                   settings, _link(settings, "/layout"), "Open the Layout page",
                   problems=problems, summary=summary)


def user_pending_message(name: str, upn: str, settings: Settings) -> Rendered:
    return _render("user_pending", f"{settings.server_name}: user waiting for approval",
                   settings, _link(settings, "/users"), "Open the users page",
                   name=name, upn=upn)


def test_message(channel_name: str, settings: Settings) -> Rendered:
    return _render("test", f"{settings.server_name}: test notification", settings,
                   _link(settings), channel=channel_name)
