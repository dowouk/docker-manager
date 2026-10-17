import re
from collections.abc import Iterable
from dataclasses import dataclass

_TAG_RE = re.compile(r"^(v?)(\d+(?:\.\d+)*)(.*)$")

# Matched case-insensitively as whole letter runs (not inside longer words, so
# "-source", "-percona" or "-developer" are fine; "-rc1", "1.0rc1", "-devel" are not).
_PRERELEASE_RE = re.compile(
    r"(?<![a-z])(rc|beta|alpha|dev|devel|nightly|preview|snapshot)(?![a-z])"
)


@dataclass(frozen=True)
class ParsedTag:
    prefix: str
    parts: tuple[int, ...]
    suffix: str


@dataclass(frozen=True)
class VersionUpdates:
    minor: str | None
    major: str | None


def parse_version_tag(tag: str) -> ParsedTag | None:
    match = _TAG_RE.match(tag)
    if match is None:
        return None
    prefix, numbers, suffix = match.groups()
    return ParsedTag(prefix, tuple(int(p) for p in numbers.split(".")), suffix)


def _is_prerelease(tag: str) -> bool:
    return _PRERELEASE_RE.search(tag.lower()) is not None


def _first_part_digits(tag: str) -> int:
    parsed = parse_version_tag(tag)
    assert parsed is not None
    return len(str(parsed.parts[0]))


def find_version_updates(current: str, tags: Iterable[str]) -> VersionUpdates:
    cur = parse_version_tag(current)
    if cur is None:
        return VersionUpdates(None, None)
    cur_digits = _first_part_digits(current)
    # An owner following a pre-release channel (e.g. 1.0-dev) keeps that channel.
    filter_prereleases = not _is_prerelease(current)

    best_minor: tuple[tuple[int, ...], str] | None = None
    best_major: tuple[tuple[int, ...], str] | None = None
    for tag in tags:
        cand = parse_version_tag(tag)
        if (
            cand is None
            or cand.prefix != cur.prefix
            or cand.suffix != cur.suffix
            or len(cand.parts) != len(cur.parts)
            or (filter_prereleases and _is_prerelease(tag))
            or _first_part_digits(tag) not in (cur_digits, cur_digits + 1)
            or cand.parts <= cur.parts
        ):
            continue
        if cand.parts[0] == cur.parts[0]:
            if best_minor is None or cand.parts > best_minor[0]:
                best_minor = (cand.parts, tag)
        elif best_major is None or cand.parts > best_major[0]:
            best_major = (cand.parts, tag)
    return VersionUpdates(
        best_minor[1] if best_minor else None,
        best_major[1] if best_major else None,
    )
