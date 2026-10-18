"""Command line: ``docker-manager reset-admin <email>`` (run inside the container).

Creates or resets a *local* Admin and prints a one-time password. It is the way back in
when everyone is locked out, so it works straight from the data folder and key file.
"""
import argparse
import os
import secrets
import string
import sys

from app.config import Settings
from app.keys import load_secret_box
from app.passwords import hash_password, password_problem
from app.settings_service import key_path
from app.setup import TOKEN_FILE, _valid_email
from app.store import Store
from app.users import UserStore

PASSWORD_LENGTH = 20
_ALPHABET = string.ascii_letters + string.digits


def _one_time_password() -> str:
    while True:
        password = "".join(secrets.choice(_ALPHABET) for _ in range(PASSWORD_LENGTH))
        if password_problem(password) is None:
            return password


def reset_admin(email: str, settings: Settings | None = None) -> str:
    """Create or reset the local Admin and return the new one-time password."""
    settings = settings or Settings()
    store = Store(os.path.join(settings.data_dir, "dashboard.db"))
    try:
        users = UserStore(store, load_secret_box(key_path(settings)))
        password = _one_time_password()
        hashed = hash_password(password)
        rec = users.by_email("local", email)
        if rec is None:
            rec = users.create("local", email, email.split("@")[0], "admin", pinned=True,
                               password_hash=hashed, must_change_password=True)
        else:
            users.update(rec.id, role="admin", role_pinned=True, disabled=False)
            users.set_password_hash(rec.id, hashed, True)
            users.set_totp(rec.id, None, False)  # they enrol again
            users.set_recovery_codes(rec.id, [])
            users.end_sessions(rec.id)
        store.add_audit("command line", "users", f"Admin reset from the command line: {email}")
        try:  # an open first-start setup is closed now that a user exists
            os.unlink(os.path.join(settings.data_dir, TOKEN_FILE))
        except OSError:
            pass
        return password
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docker-manager")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-admin", help="create or reset a local Admin")
    reset.add_argument("email")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    email = args.email.strip()
    if not _valid_email(email):
        print("Give a valid email address: docker-manager reset-admin <email>", file=sys.stderr)
        return 2
    password = reset_admin(email)
    print(f"One-time password: {password}")
    print("Sign in with it; you will be asked to choose a new password and set up "
          "two-step sign-in again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
