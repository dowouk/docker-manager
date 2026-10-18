import stat

import pytest

from app.keys import SecretBox, load_or_create_key, read_key


def test_creates_key_once_with_0600(tmp_path):
    path = tmp_path / "sub" / "secret.key"
    key = load_or_create_key(str(path))
    assert len(key) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_key(str(path)) == key


def test_rejects_wrong_length(tmp_path):
    path = tmp_path / "secret.key"
    path.write_bytes(b"short")
    with pytest.raises(ValueError):
        load_or_create_key(str(path))


def test_read_key_never_creates(tmp_path):
    path = tmp_path / "secret.key"
    assert read_key(str(path)) is None
    assert not path.exists()
    path.write_bytes(b"short")
    assert read_key(str(path)) is None
    path.write_bytes(b"k" * 32)
    assert read_key(str(path)) == b"k" * 32


def test_round_trip_and_wrong_key():
    box = SecretBox(b"a" * 32)
    token = box.encrypt("hunter2-connection-string")
    assert "hunter2" not in token
    assert box.decrypt(token) == "hunter2-connection-string"
    assert SecretBox(b"b" * 32).decrypt(token) is None
    assert box.decrypt("not-a-token") is None


def test_read_key_unreadable_is_none(tmp_path):
    assert read_key(str(tmp_path)) is None  # a directory: OSError, not FileNotFoundError


def test_load_secret_box_with_bad_key_file_does_not_crash_or_overwrite(tmp_path, caplog):
    from app.keys import KeyUnusable, load_secret_box

    path = tmp_path / "secret.key"
    path.write_bytes(b"short")
    box = load_secret_box(str(path))
    assert path.read_bytes() == b"short"
    assert caplog.text.count("secret key") == 1
    assert box.decrypt(SecretBox(b"a" * 32).encrypt("x")) is None
    with pytest.raises(KeyUnusable):
        box.encrypt("x")


def test_load_secret_box_good_and_missing(tmp_path):
    from app.keys import load_secret_box

    path = tmp_path / "new" / "secret.key"
    box = load_secret_box(str(path))
    assert path.exists()
    assert box.decrypt(box.encrypt("x")) == "x"
