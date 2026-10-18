"""Presentation helpers for the dashboard: pure functions, no I/O.

Every container gets one *state* that drives the filter buttons, the stat cards
and the "Needs attention" cards, so the numbers on the page always agree:

- "error"     – the check failed
- "update"    – a newer build, or a newer minor or major version, is available
- "current"   – up to date
- "unchecked" – can't be checked: a local image, or one pinned by digest. Neutral:
                not a problem, and left out of the fleet-health percentage.
"""
import re
from dataclasses import dataclass
from datetime import datetime

from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status
from app.imageref import parse_image_ref

BUILD_UPDATE = (Status.NEWER_BUILD, Status.PULLED_NOT_RECREATED)
_ORDER = {"update": 0, "error": 1, "current": 2, "unchecked": 3}


def has_update(cs: ContainerStatus) -> bool:
    return cs.status in BUILD_UPDATE or bool(cs.minor_update or cs.major_update)


def row_state(cs: ContainerStatus) -> str:
    if cs.status == Status.NOT_CHECKABLE:
        return "unchecked"
    if cs.status == Status.ERROR:
        return "error"
    if has_update(cs):
        return "update"
    return "current"


def mailcow_state(mc: MailcowStatus) -> str:
    if mc.error:
        return "error"
    if mc.update_available:
        return "update"
    return "current"


def by_urgency(result: CheckResult) -> list[ContainerStatus]:
    """Updates first, then problems, then what's current, then what can't be checked; by name within each."""
    return sorted(result.containers, key=lambda cs: (_ORDER[row_state(cs)], cs.container.name))


@dataclass
class Overview:
    total: int
    stacks: int
    current: int
    updates: int
    majors: int
    errors: int
    unchecked: int

    @property
    def attention(self) -> int:
        return self.updates + self.errors

    @property
    def percent_current(self) -> int:
        checked = self.total - self.unchecked
        return round(100 * self.current / checked) if checked else 0


def overview(result: CheckResult) -> Overview:
    states = [row_state(cs) for cs in result.containers]
    if result.mailcow:
        states.append(mailcow_state(result.mailcow))
    stacks = {cs.container.project or cs.container.name for cs in result.containers}
    return Overview(
        total=len(states),
        stacks=len(stacks) + (1 if result.mailcow else 0),
        current=states.count("current"),
        updates=states.count("update"),
        majors=sum(1 for cs in result.containers if cs.major_update),
        errors=states.count("error"),
        unchecked=states.count("unchecked"),
    )


@dataclass
class Attention:
    """One "Needs attention" card. `cs` is None for the Mailcow card."""
    state: str
    badge: str
    tag: str
    name: str
    text: str
    cs: ContainerStatus | None = None


def _error_badge(cs: ContainerStatus) -> str:
    message = (cs.message or "").lower()
    if "rate limit" in message:
        return "Rate limited"
    if re.search(r"\b401\b", message):
        return "Auth failed · 401"
    return "Error"


def _registry(image_ref: str) -> str:
    try:
        return parse_image_ref(image_ref).registry
    except ValueError:
        return image_ref


def _update_card(cs: ContainerStatus) -> Attention:
    c = cs.container
    if cs.status == Status.NEWER_BUILD:
        badge, text = "Newer build", "A newer build of the same tag is ready to pull."
    elif cs.status == Status.PULLED_NOT_RECREATED:
        badge, text = "Pulled", "The newer image is pulled, but the container still runs the old one."
    elif cs.minor_update:
        badge, text = "New version", f"Version {cs.minor_update} is available."
    else:
        badge, text = "Major version", f"Version {cs.major_update} is available – update by hand."
    return Attention("update", badge, c.image_ref, c.name, text, cs)


def attention(result: CheckResult) -> list[Attention]:
    cards: list[Attention] = []
    mc = result.mailcow
    if mc and mailcow_state(mc) == "update":
        cards.append(Attention("update", "Update available", mc.latest_release or "mailcow", "Mailcow",
                               f"Installed {mc.current_version or 'unknown'} – the newest release is "
                               f"{mc.latest_release or 'unknown'}."))
    for cs in by_urgency(result):
        state = row_state(cs)
        if state == "update":
            cards.append(_update_card(cs))
        elif state == "error":
            text = cs.message or cs.not_updatable_reason or "The check failed."
            cards.append(Attention("error", _error_badge(cs), _registry(cs.container.image_ref),
                                   cs.container.name, text, cs))
    if mc and mailcow_state(mc) == "error":
        cards.append(Attention("error", "Error", "mailcow", "Mailcow", mc.error or "The check failed."))
    return cards


def initials(name: str) -> str:
    """Two letters for a container tile: the first letters of the first two words,
    or the first two characters of a single word ("uptime-kuma" → "uk", "caddy" → "ca")."""
    words = [w for w in re.split(r"[-_.\s]+", name) if w]
    if not words:
        return "?"
    if len(words) >= 2 and words[1][0].isalpha():
        return (words[0][0] + words[1][0]).lower()
    return words[0][:2].lower()


def user_initials(name: str) -> str:
    words = name.split()
    if not words:
        return "?"
    letters = words[0][0] + (words[-1][0] if len(words) > 1 else "")
    return letters.upper()


def first_name(name: str) -> str:
    words = name.split()
    return words[0] if words else "there"


def greeting(now: datetime) -> str:
    if now.hour < 12:
        return "Good morning"
    if now.hour < 18:
        return "Good afternoon"
    return "Good evening"
