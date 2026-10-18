"""Docker credential helper: `docker-credential-docker-manager`.

Docker runs this for each registry named in credHelpers, so `docker compose pull`
gets registry logins without any password being written to a file. It decrypts them
from the database on demand. A missing or unreadable key means "not found" – it must
never create a key, which would make every saved secret unreadable."""
import json
import os
import pathlib
import sqlite3
import sys

from app.keys import SecretBox, read_key
from app.registry_logins import HELPER_CONFIG, auth_file_logins, normalise_host
from app.settings_service import REGISTRY_LOGINS, logins_from_stored

NOT_FOUND = "credentials not found in native keychain"


def _saved_logins(db: str, key: str) -> list[dict]:
    secret = read_key(key)
    if secret is None or not os.path.exists(db):
        return []
    # Read-only: the helper must never run migrations or write the live database.
    conn = sqlite3.connect(f"{pathlib.Path(db).absolute().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT value FROM settings WHERE name = ?", (REGISTRY_LOGINS,)).fetchone()
    finally:
        conn.close()
    return logins_from_stored(json.loads(row[0]) if row else None, SecretBox(secret))


def _logins(config_dir: str | None) -> dict[str, tuple[str, str]]:
    """Normalised host -> (username, password): saved logins first, then the auth file."""
    if not config_dir:
        return {}
    try:
        with open(os.path.join(config_dir, HELPER_CONFIG), encoding="utf-8") as fh:
            cfg = json.load(fh)
        db, key, auth_file = cfg["db"], cfg["key"], cfg.get("auth_file")
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    found = auth_file_logins(auth_file)
    try:
        saved = _saved_logins(db, key)
    except (sqlite3.Error, ValueError):  # locked, corrupt or not our schema: just "not found"
        saved = []
    for item in saved:
        try:
            found[normalise_host(item["host"])] = (item["username"], item["password"])
        except (KeyError, TypeError, ValueError):
            continue
    return found


def main(argv: list[str] | None = None, stdin=sys.stdin, stdout=sys.stdout) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else ""
    if command == "get":
        url = stdin.read().strip()
        try:
            user, secret = _logins(os.environ.get("DOCKER_CONFIG"))[normalise_host(url)]
        except (KeyError, ValueError):
            stdout.write(NOT_FOUND + "\n")
            return 1
        stdout.write(json.dumps({"ServerURL": url, "Username": user, "Secret": secret}) + "\n")
        return 0
    if command == "list":
        listing = {host: user for host, (user, _) in _logins(os.environ.get("DOCKER_CONFIG")).items()}
        stdout.write(json.dumps(listing) + "\n")
        return 0
    if command in ("store", "erase"):
        stdin.read()
        return 0  # logins are managed on the Settings page; `docker login` must not fail
    sys.stderr.write("usage: docker-credential-docker-manager get|store|erase|list\n")
    return 2


def main_cli() -> None:
    sys.exit(main())
