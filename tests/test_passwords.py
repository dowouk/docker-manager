from importlib import resources

from app import passwords
from app.passwords import (
    DUMMY_HASH, MIN_LENGTH, hash_password, needs_rehash, password_problem, verify_password,
)


def _list_path():
    return resources.files("app") / "data" / "common-passwords.txt"


def test_hash_and_verify():
    stored = hash_password("correct horse battery")
    assert stored != "correct horse battery"
    assert verify_password(stored, "correct horse battery") is True
    assert verify_password(stored, "wrong horse battery") is False
    assert verify_password("not-a-hash", "correct horse battery") is False
    assert verify_password("", "x") is False
    assert needs_rehash(stored) is False
    assert verify_password(DUMMY_HASH, "anything") is False


def test_too_short():
    assert MIN_LENGTH == 12
    assert password_problem("abcdefghijk") == "Use at least 12 characters"


def test_common_rejected_case_insensitive():
    entry = next(
        line.strip() for line in _list_path().read_text(encoding="utf-8").splitlines()
        if len(line.strip()) >= 12
    )
    assert password_problem(entry.upper()) == "That password is too common – choose another"


def test_ok():
    assert password_problem("plum-orbit-vessel-42") is None


def test_list_is_packaged():
    lines = _list_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) > 9000
    assert passwords._common()  # loaded lazily, lower-cased
