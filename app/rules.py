"""Per-container watch rules: set by `docker-manager.*` labels or on the dashboard."""
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import re
import time

import regex

from app.inventory import ContainerInfo, container_key
from app.store import Store
from app.versions import parse_version_tag

SETTING = "container_rules"
MAX_PATTERNS = 10
MAX_PATTERN_LENGTH = 200
MATCH_TIMEOUT = 0.05
MAX_URL_LENGTH = 500
MATCH_BUDGET = 0.5  # CPU seconds of matching per container once a match was slow or timed out
MATCH_CEILING = 2.0  # CPU seconds of matching per container, always
SLOW_MATCH = 0.01  # one match slower than this engages the budget
MAX_COUNT = 20

_BOOLS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}
# field -> label name
LABELS = {
    "watch": "docker-manager.enable",
    "include_tags": "docker-manager.include_tags",
    "exclude_tags": "docker-manager.exclude_tags",
    "notify": "docker-manager.notify",
    "release_notes": "docker-manager.release_notes",
    "layout_ignore": "docker-manager.layout.ignore",
}
_BOOL_FIELDS = ("watch", "notify", "layout_ignore")
_LIST_FIELDS = ("include_tags", "exclude_tags")


@dataclass(frozen=True)
class Rules:
    watch: bool = True
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    notify: bool = True
    release_notes: str | None = None
    layout_ignore: bool = False
    from_label: frozenset[str] = frozenset()
    warnings: tuple[str, ...] = ()


class RulesError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def _parse_bool(text: str) -> bool:
    try:
        return _BOOLS[text.strip().lower()]
    except KeyError:
        raise ValueError("use true or false") from None


_COUNTED = re.compile(r"\{(\d+)(?:,(\d+))?\}")


UNSUPPORTED = (
    "This pattern uses something tag patterns don't support "
    "(for example \\p{...}, \\N{...}, inline flags, lookarounds or backreferences)"
)
_LITERALS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
_ESCAPES = frozenset(".-\\dw{}()[]|+*?^$/")  # \d and \w, or an escaped special character
_CLASS_ESCAPES = frozenset("dw]-\\")


def _scan_class(pattern: str, i: int) -> int:
    """`i` is just after '['. Returns the index after the closing ']'."""
    n = len(pattern)
    if pattern[i: i + 1] == "^":
        i += 1
    start = i
    while i < n:
        ch = pattern[i]
        if ch == "]":
            if i == start:
                raise ValueError(UNSUPPORTED)  # an empty class
            return i + 1
        if ch == "\\":
            if pattern[i + 1: i + 2] not in _CLASS_ESCAPES or i + 1 >= n:
                raise ValueError(UNSUPPORTED)
            i += 2
        elif ch in _LITERALS:
            i += 1
        else:
            raise ValueError(UNSUPPORTED)  # includes '[', ':' and '='
    raise ValueError(UNSUPPORTED)


def _check_syntax(pattern: str) -> None:
    """An allow-list check done BEFORE compiling (the match timeout can't stop a pattern
    that blows up the compiler). Accepts only: literals [A-Za-z0-9._-], a few escapes,
    simple character classes, groups ( and (?: with |, anchors ^ $, and the quantifiers
    * + ? {n} {n,m} (n, m <= MAX_COUNT) – never stacked. A group holding a repeat may only
    be repeated by {n,m}, and not if it holds a {n,m} itself. Anything else is refused."""
    # per open group: [contains a quantifier, contains a counted repeat {n}]
    frames: list[list[bool]] = []
    atom = None  # what a quantifier applies to: None, "plain", "stacked" or a closed group's flags
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        quant_end = None
        if ch in _LITERALS or ch == ".":
            i, atom = i + 1, "plain"
        elif ch == "\\":
            if pattern[i + 1: i + 2] not in _ESCAPES or i + 1 >= n:
                raise ValueError(UNSUPPORTED)
            i, atom = i + 2, "plain"
        elif ch == "[":
            i, atom = _scan_class(pattern, i + 1), "plain"
        elif ch == "(":
            if pattern.startswith("(?", i):
                if not pattern.startswith("(?:", i):
                    raise ValueError(UNSUPPORTED)
                i += 3
            else:
                i += 1
            frames.append([False, False])
            atom = None
        elif ch == ")":
            if not frames:
                raise ValueError("unbalanced parentheses")
            inner = frames.pop()
            if frames:
                frames[-1] = [frames[-1][0] or inner[0], frames[-1][1] or inner[1]]
            i, atom = i + 1, tuple(inner)
        elif ch == "|":
            i, atom = i + 1, None
        elif ch in "^$":
            i, atom = i + 1, None
        elif ch in "*+?":
            quant_end = i + 1
        elif ch == "{":
            m = _COUNTED.match(pattern, i)
            if m is None:
                raise ValueError(UNSUPPORTED)
            if any(g and int(g) > MAX_COUNT for g in m.groups()):
                raise ValueError(f"repeat counts can be at most {MAX_COUNT}")
            quant_end = m.end()
        else:
            raise ValueError(UNSUPPORTED)
        if quant_end is not None:
            if atom is None:
                raise ValueError("a repeat needs something to repeat")
            if atom == "stacked":
                raise ValueError("repeats can't be stacked")
            counted = ch == "{"
            if isinstance(atom, tuple) and atom[0] and (not counted or atom[1]):
                raise ValueError("a repeated group can't contain another repeat")
            if frames:
                frames[-1] = [True, frames[-1][1] or counted]
            i, atom = quant_end, "stacked"  # a further quantifier would be stacked
    if frames:
        raise ValueError("unbalanced parentheses")


def _parse_list(text: str, dropped: list[str] | None = None) -> list[str]:
    """Split, size-check and syntax-check. A pattern failing the syntax check raises, or,
    when `dropped` is given (labels), is left out with a warning added to it."""
    patterns = [p.strip() for p in text.split(";") if p.strip()]
    if len(patterns) > MAX_PATTERNS:
        raise ValueError(f"at most {MAX_PATTERNS} patterns")
    if any(len(p) > MAX_PATTERN_LENGTH for p in patterns):
        raise ValueError(f"each pattern can be at most {MAX_PATTERN_LENGTH} characters")
    kept = []
    for p in patterns:
        try:
            _check_syntax(p)
        except ValueError as exc:
            if dropped is None:
                raise ValueError(f"'{p}': {exc}") from None
            dropped.append(f"Tag pattern '{p}' ignored: {exc}")
        else:
            kept.append(p)
    return kept


_CONTROL = re.compile(r"[\s\x00-\x1f\x7f\\]")


def is_plain_http_url(text: str) -> bool:
    """True for `http(s)://host/...` only: no whitespace, control characters or backslashes,
    no user info (`@` in the host part) and no scheme without slashes."""
    if _CONTROL.search(text) or not re.match(r"https?://", text, re.I):
        return False
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return False
    return bool(host) and "@" not in parts.netloc


def _parse_url(text: str) -> str:
    text = text.strip()
    if len(text) > MAX_URL_LENGTH or not is_plain_http_url(text):
        raise ValueError("use a link starting with https:// or http://")
    return text


def _parse(field: str, text: str, dropped: list[str] | None = None):
    if field in _BOOL_FIELDS:
        return _parse_bool(text)
    if field in _LIST_FIELDS:
        return _parse_list(text, dropped)
    return _parse_url(text)


def _parse_saved(field: str, value: object, dropped: list[str]):
    """A stored value, type-checked like a submitted one (the row may be hand-edited)."""
    if field in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise ValueError("not true or false")
        return value
    if field in _LIST_FIELDS:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("not a list of patterns")
        return _parse_list(";".join(value), dropped)
    if not isinstance(value, str):
        raise ValueError("not a link")
    return _parse_url(value)


def _check_compiles(patterns: list[str]) -> None:
    for p in patterns:
        try:
            regex.compile(p)
        except regex.error as exc:
            raise ValueError(f"'{p}' is not a valid pattern ({exc})") from None


class TagFilter:
    """Applies one container's include/exclude patterns. A pattern that times out is
    switched off for the rest of the check, and matching stops after MATCH_BUDGET seconds
    (the remaining tags then count as not allowed). Warnings collect in `warnings`."""

    def __init__(self, rules: Rules, warnings: list[str] | None = None):
        self._rules = rules
        self.warnings = warnings if warnings is not None else []
        self._off: set[str] = set()
        self._spent = 0.0
        self._slow = False
        self._over = False

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def _matches(self, pattern: str, tag: str) -> bool:
        if pattern in self._off:
            return False
        started = time.thread_time()
        try:
            return regex.fullmatch(pattern, tag, timeout=MATCH_TIMEOUT, concurrent=True) is not None
        except (TimeoutError, regex.error, ValueError, OverflowError) as exc:
            self._off.add(pattern)
            self._slow = self._slow or isinstance(exc, TimeoutError)
            reason = "took too long" if isinstance(exc, TimeoutError) else "is not valid"
            self._warn(f"Tag pattern '{pattern}' {reason} and was treated as no match")
            return False
        finally:
            if time.thread_time() - started > SLOW_MATCH:
                self._slow = True

    def allowed(self, tag: str) -> bool:
        rules = self._rules
        if not rules.include_tags and not rules.exclude_tags:
            return True
        if self._over:
            return False
        started = time.thread_time()  # CPU time of this thread: other threads don't count
        try:
            if rules.include_tags and not any(self._matches(p, tag) for p in rules.include_tags):
                return False
            return not any(self._matches(p, tag) for p in rules.exclude_tags)
        finally:
            self._spent += time.thread_time() - started
            # Cheap patterns never truncate: the budget only applies once a match was slow
            # or timed out; the ceiling always applies.
            limit = MATCH_BUDGET if self._slow else MATCH_CEILING
            if self._spent > min(limit, MATCH_CEILING) and not self._over:
                self._over = True
                self._warn("Tag patterns took too long – the remaining tags were not considered")

    def filter(self, tags: list[str]) -> list[str]:
        """Only version-like tags can be update candidates, so only those are matched."""
        return [t for t in tags if parse_version_tag(t) is not None and self.allowed(t)]


def tag_allowed(tag: str, rules: Rules, warnings: list[str] | None = None) -> bool:
    """Include (if any) must match the whole tag; exclude must not. A pattern that can't
    be compiled or takes too long counts as no match and adds a warning."""
    return TagFilter(rules, warnings).allowed(tag)


class RulesStore:
    def __init__(self, store: Store):
        self._store = store

    def _all(self) -> dict[str, dict]:
        raw = self._store.load_settings().get(SETTING)
        return raw if isinstance(raw, dict) else {}

    def saved(self, key: str) -> dict:
        value = self._all().get(key)
        return dict(value) if isinstance(value, dict) else {}  # may be hand-edited: see _effective

    def save(self, key: str, form: Mapping[str, str], user: str) -> None:
        """Validate the submitted fields and store them; fields not submitted keep their
        saved value (a field set by a label is shown disabled, so isn't submitted)."""
        values = self.saved(key)
        errors: dict[str, str] = {}
        for field in LABELS:
            if field not in form:
                continue
            text = str(form[field])
            try:
                if field == "release_notes" and not text.strip():
                    values.pop(field, None)
                    continue
                parsed = _parse(field, text)
                if field in _LIST_FIELDS:
                    _check_compiles(parsed)
            except ValueError as exc:
                errors[field] = str(exc)
                continue
            values[field] = parsed
        if errors:
            raise RulesError(errors)
        everything = self._all()
        everything[key] = values
        self._store.save_settings({SETTING: everything})
        self._store.add_audit(user, "updates", f"Watch rules changed: {key}")

    def snapshot(self):
        """`effective` bound to the stored rules as they are now (one read per check)."""
        everything = self._all()
        return lambda info: self._effective(info, everything.get(container_key(info)))

    def effective(self, info: ContainerInfo) -> Rules:
        return self._effective(info, self._all().get(container_key(info)))

    @staticmethod
    def _effective(info: ContainerInfo, saved_raw: object) -> Rules:
        saved = saved_raw if isinstance(saved_raw, dict) else {}
        values: dict[str, object] = {}
        from_label: set[str] = set()
        warnings: list[str] = []
        for field, label in LABELS.items():
            if label in info.labels:
                try:
                    parsed = _parse(field, info.labels[label], warnings)
                except ValueError as exc:
                    warnings.append(f"Label {label} ignored: {exc}")
                else:
                    values[field] = tuple(parsed) if field in _LIST_FIELDS else parsed
                    from_label.add(field)
                    continue
            if field in saved:
                try:
                    parsed = _parse_saved(field, saved[field], warnings)
                except ValueError as exc:
                    warnings.append(f"Saved {field.replace('_', ' ')} ignored: {exc}")
                else:
                    values[field] = tuple(parsed) if field in _LIST_FIELDS else parsed
        return Rules(**values, from_label=frozenset(from_label), warnings=tuple(warnings))

