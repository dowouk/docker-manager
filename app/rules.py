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
MATCH_BUDGET = 0.5  # seconds of pattern matching allowed per container per check
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


_COUNTED = re.compile(r"\{(\d*)(?:(,)(\d*))?\}")
_FLAG_GROUP = re.compile(r"\(\?[a-zA-Z]+(?:-[a-zA-Z]+)?[:)]")


def _check_syntax(pattern: str) -> None:
    """A conservative check done BEFORE compiling: the match timeout can't stop a pattern
    that blows up the compiler (nested counted repeats). Rejects counted repeats above
    MAX_COUNT, a quantifier applied to a group that already holds one, backreferences,
    lookarounds, conditionals and recursion."""
    frames: list[bool] = []  # per open group: does it contain a quantifier?
    last_group_quantified = False  # the atom just before is a group holding a quantifier
    after_quant = False  # the previous token was a quantifier (so +/? are modifiers)
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        quant_end = None
        if ch == "\\":
            nxt = pattern[i + 1: i + 2]
            if nxt.isdigit() and nxt != "0" or nxt in ("g", "k"):
                raise ValueError("backreferences are not supported")
            i += 2
            last_group_quantified = after_quant = False
            continue
        if ch == "[":
            j = i + 1
            if pattern[j: j + 1] == "^":
                j += 1
            if pattern[j: j + 1] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
            last_group_quantified = after_quant = False
            continue
        if ch == "(":
            if pattern.startswith("(?", i):
                flag = _FLAG_GROUP.match(pattern, i)
                if flag is None and not (
                    pattern.startswith("(?:", i)
                    or pattern.startswith("(?P<", i)
                    or (pattern.startswith("(?<", i) and pattern[i + 3: i + 4] not in ("=", "!"))
                ):
                    raise ValueError("lookarounds, conditionals and recursion are not supported")
            frames.append(False)
            i += 1
            last_group_quantified = after_quant = False
            continue
        if ch == ")":
            inner = frames.pop() if frames else False
            if frames:
                frames[-1] = frames[-1] or inner
            last_group_quantified, after_quant = inner, False
            i += 1
            continue
        if ch in "*+?":
            if after_quant and ch in "+?":
                after_quant = False  # lazy or possessive modifier
                i += 1
                continue
            quant_end = i + 1
        elif ch == "{":
            m = _COUNTED.match(pattern, i)
            if m and (m.group(1) or m.group(3)):
                if any(g and int(g) > MAX_COUNT for g in (m.group(1), m.group(3))):
                    raise ValueError(f"repeat counts can be at most {MAX_COUNT}")
                quant_end = m.end()
        if quant_end is not None:
            if after_quant or last_group_quantified:
                raise ValueError("a repeated group can't contain another repeat")
            if frames:
                frames[-1] = True
            i, after_quant, last_group_quantified = quant_end, True, False
            continue
        i += 1
        last_group_quantified = after_quant = False
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


def _parse_url(text: str) -> str:
    text = text.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        raise ValueError("not a valid URL") from None
    if (parts.scheme not in ("http", "https") or not parts.netloc
            or len(text) > MAX_URL_LENGTH or any(ch.isspace() for ch in text)):
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
        self._over = False

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def _matches(self, pattern: str, tag: str) -> bool:
        if pattern in self._off:
            return False
        try:
            return regex.fullmatch(pattern, tag, timeout=MATCH_TIMEOUT, concurrent=True) is not None
        except (TimeoutError, regex.error, ValueError, OverflowError) as exc:
            self._off.add(pattern)
            reason = "took too long" if isinstance(exc, TimeoutError) else "is not valid"
            self._warn(f"Tag pattern '{pattern}' {reason} and was treated as no match")
            return False

    def allowed(self, tag: str) -> bool:
        rules = self._rules
        if not rules.include_tags and not rules.exclude_tags:
            return True
        if self._over:
            return False
        started = time.monotonic()
        try:
            if rules.include_tags and not any(self._matches(p, tag) for p in rules.include_tags):
                return False
            return not any(self._matches(p, tag) for p in rules.exclude_tags)
        finally:
            self._spent += time.monotonic() - started
            if self._spent > MATCH_BUDGET and not self._over:
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

