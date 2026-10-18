"""Password hashing (argon2id) and the minimum-length / common-password policy."""
from functools import lru_cache
from importlib import resources

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 12

_hasher = PasswordHasher()  # argon2id with the library's current defaults

# Hashed once at import so a login for an unknown account can burn the same work.
DUMMY_HASH = _hasher.hash("docker-manager-dummy-password")


def hash_password(plain: str) -> str:
    try:
        return _hasher.hash(plain)
    except UnicodeEncodeError as exc:  # lone surrogate: not encodable as UTF-8
        raise ValueError("Password contains invalid characters") from exc


def verify_password(stored_hash: str, plain: str) -> bool:
    if not isinstance(stored_hash, str) or not stored_hash or not isinstance(plain, str):
        return False
    try:
        return _hasher.verify(stored_hash, plain)
    except (VerificationError, InvalidHashError, TypeError, ValueError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    # An invalid hash counts as needing a rehash (returns True).
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def _common() -> frozenset[str]:
    text = (resources.files("app") / "data" / "common-passwords.txt").read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def password_problem(plain: str) -> str | None:
    if len(plain) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters"
    if plain.lower() in _common():
        return "That password is too common – choose another"
    return None
