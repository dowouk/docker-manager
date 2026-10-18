"""Per-container watch rules: set by `docker-manager.*` labels or on the dashboard."""
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import regex

from app.inventory import ContainerInfo, container_key
from app.store import Store

SETTING = "container_rules"
MAX_PATTERNS = 10
MAX_PATTERN_LENGTH = 200
MATCH_TIMEOUT = 0.05
MAX_URL_LENGTH = 500

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


def _parse_list(text: str) -> list[str]:
    patterns = [p.strip() for p in text.split(";") if p.strip()]
    if len(patterns) > MAX_PATTERNS:
        raise ValueError(f"at most {MAX_PATTERNS} patterns")
    if any(len(p) > MAX_PATTERN_LENGTH for p in patterns):
        raise ValueError(f"each pattern can be at most {MAX_PATTERN_LENGTH} characters")
    return patterns


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


def _parse(field: str, text: str):
    if field in _BOOL_FIELDS:
        return _parse_bool(text)
    if field in _LIST_FIELDS:
        return _parse_list(text)
    return _parse_url(text)


def _check_compiles(patterns: list[str]) -> None:
    for p in patterns:
        try:
            regex.compile(p)
        except regex.error as exc:
            raise ValueError(f"'{p}' is not a valid pattern ({exc})") from None


def tag_allowed(tag: str, rules: Rules, warnings: list[str] | None = None) -> bool:
    """Include (if any) must match the whole tag; exclude must not. A pattern that can't
    be compiled or takes too long counts as no match and adds a warning."""

    def matches(pattern: str) -> bool:
        try:
            return regex.fullmatch(pattern, tag, timeout=MATCH_TIMEOUT) is not None
        except (TimeoutError, regex.error, ValueError, OverflowError) as exc:
            reason = "took too long" if isinstance(exc, TimeoutError) else "is not valid"
            message = f"Tag pattern '{pattern}' {reason} and was treated as no match"
            if warnings is not None and message not in warnings:
                warnings.append(message)
            return False

    if rules.include_tags and not any(matches(p) for p in rules.include_tags):
        return False
    return not any(matches(p) for p in rules.exclude_tags)


class RulesStore:
    def __init__(self, store: Store):
        self._store = store

    def _all(self) -> dict[str, dict]:
        raw = self._store.load_settings().get(SETTING)
        return raw if isinstance(raw, dict) else {}

    def saved(self, key: str) -> dict:
        value = self._all().get(key)
        return dict(value) if isinstance(value, dict) else {}

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

    def effective(self, info: ContainerInfo) -> Rules:
        saved = self.saved(container_key(info))
        values: dict[str, object] = {}
        from_label: set[str] = set()
        warnings: list[str] = []
        for field, label in LABELS.items():
            if label in info.labels:
                try:
                    parsed = _parse(field, info.labels[label])
                except ValueError as exc:
                    warnings.append(f"Label {label} ignored: {exc}")
                else:
                    values[field] = tuple(parsed) if field in _LIST_FIELDS else parsed
                    from_label.add(field)
                    continue
            if field in saved:
                value = saved[field]
                values[field] = tuple(value) if field in _LIST_FIELDS else value
        return Rules(**values, from_label=frozenset(from_label), warnings=tuple(warnings))

