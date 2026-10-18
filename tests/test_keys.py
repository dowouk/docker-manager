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
