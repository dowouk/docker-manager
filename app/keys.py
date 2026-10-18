"""The key file and the encryption of secrets stored in the database."""

import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_BYTES = 32


def read_key(path: str) -> bytes | None:
    """The key, or None if the file is missing or not exactly 32 bytes."""
    try:
        with open(path, "rb") as f:
            key = f.read()
    except FileNotFoundError:
        return None
    return key if len(key) == KEY_BYTES else None


def load_or_create_key(path: str) -> bytes:
    key = read_key(path)
    if key is not None:
        return key
    if os.path.exists(path):
        raise ValueError(f"{path} exists but is not {KEY_BYTES} bytes")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = os.urandom(KEY_BYTES)
    try:
        # O_EXCL so two starting processes never overwrite each other's key
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_or_create_key(path)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class SecretBox:
    def __init__(self, key: bytes):
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"docker-manager settings",
        ).derive(key)
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None
