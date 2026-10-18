"""docker-manager reset-admin and the entry point."""
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest

from app import cli
from app.keys import SecretBox
from app.passwords import hash_password, verify_password
from app.store import Store
from app.users import UserStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / "key"))
    for k in ("PUBLIC_URL", "AZURE_TENANT_ID"):
        monkeypatch.delenv(k, raising=False)


def users_of(tmp_path):
    store = Store(str(tmp_path / "dashboard.db"))
    return store, UserStore(store, SecretBox(b"k" * 32))


def test_entry_point_declared():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["docker-manager"] == "app.cli:main"


def test_creates_a_local_admin_and_prints_password(tmp_path, capsys):
    assert cli.main(["reset-admin", "Owner@Example.com"]) == 0
    out = capsys.readouterr().out
    password = out.split("One-time password: ")[1].split()[0]
    assert len(password) == 20
    store, users = users_of(tmp_path)
    rec = users.by_email("local", "owner@example.com")
    assert rec.role == "admin" and rec.role_pinned and rec.must_change_password
    assert not rec.disabled and verify_password(rec.password_hash, password)
    assert any("Admin reset from the command line: Owner@Example.com" in e.summary
               for e in store.list_audit())
    assert password not in " ".join(e.summary for e in store.list_audit())
    lines = out.splitlines()
    assert len(lines) == 2 and lines[0] == f"One-time password: {password}"
    assert lines[1].startswith("Sign in with it;")


def test_clears_the_accounts_failures_and_lock(tmp_path, capsys):
    store, users = users_of(tmp_path)
    for _ in range(5):
        users.record_failure("account", "a@example.com")
    users.record_failure("account-lock", "a@example.com")
    users.record_failure("ip", "1.2.3.4")
    store.close()

    assert cli.main(["reset-admin", "A@Example.com"]) == 0
    capsys.readouterr()
    store, users = users_of(tmp_path)
    assert users.failures("account", "a@example.com", timedelta(minutes=15)) == 0
    assert users.failures("account-lock", "a@example.com", timedelta(minutes=15)) == 0
    assert users.failures("ip", "1.2.3.4", timedelta(minutes=15)) == 1  # an address block is left alone


def test_resets_an_existing_account(tmp_path, capsys):
    store, users = users_of(tmp_path)
    rec = users.create("local", "a@example.com", "A", "viewer",
                       password_hash=hash_password("old password here"))
    users.update(rec.id, disabled=True)
    users.create("local", "other@example.com", "O", "admin")  # keeps last-admin rules happy
    users.set_totp(rec.id, "JBSWY3DPEHPK3PXP", True)
    users.set_recovery_codes(rec.id, ["x"])
    from app.sessions import SessionStore
    sessions = SessionStore(store)
    token = sessions.create({"user_id": rec.id})
    store.close()

    assert cli.main(["reset-admin", "a@example.com"]) == 0
    password = capsys.readouterr().out.split("One-time password: ")[1].split()[0]

    store, users = users_of(tmp_path)
    got = users.by_email("local", "a@example.com")
    assert got.id == rec.id and got.role == "admin" and not got.disabled
    assert got.must_change_password and not got.totp_enabled and got.totp_secret is None
    assert got.recovery_codes == []
    assert verify_password(got.password_hash, password)
    assert not verify_password(got.password_hash, "old password here")
    assert SessionStore(store).load(token) is None
    store.close()


def test_does_not_touch_an_entra_account_of_the_same_address(tmp_path, capsys):
    store, users = users_of(tmp_path)
    users.create("entra", "a@example.com", "A", "viewer")
    store.close()
    assert cli.main(["reset-admin", "a@example.com"]) == 0
    store, users = users_of(tmp_path)
    assert users.by_email("entra", "a@example.com").role == "viewer"
    assert users.by_email("local", "a@example.com").role == "admin"
    store.close()


def test_removes_a_pending_setup_token(tmp_path, capsys):
    (tmp_path / "setup-token").write_text("t\n")
    assert cli.main(["reset-admin", "a@example.com"]) == 0
    assert not (tmp_path / "setup-token").exists()


@pytest.mark.parametrize("argv", [
    ["reset-admin"], ["reset-admin", ""], ["reset-admin", "  "], ["reset-admin", "nope"],
    ["reset-admin", "a b@example.com"], [], ["unknown"]])
def test_usage_errors_exit_2(argv, tmp_path, capsys):
    assert cli.main(argv) == 2
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "dashboard.db").exists()


def test_turns_local_sign_in_back_on(tmp_path, capsys, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    monkeypatch.setenv("PUBLIC_URL", "https://dash.example.com")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")
    store = Store(str(tmp_path / "dashboard.db"))
    store.save_settings({"local_sign_in": False})
    store.close()
    assert cli.main(["reset-admin", "a@example.com"]) == 0
    out = capsys.readouterr().out
    assert "Local sign-in was off and is now on" in out
    password = out.split("One-time password: ")[1].split()[0]
    store = Store(str(tmp_path / "dashboard.db"))
    assert store.load_settings()["local_sign_in"] is True
    assert any("Local sign-in turned on" in e.summary for e in store.list_audit())
    app = create_app(Settings(_env_file=None), docker_client=object(),
                     http=httpx.AsyncClient(), store=store, start_scheduler=False)
    assert app.state.settings.local_sign_in
    import re
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        page = c.get("/auth/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        r = c.post("/auth/login", data={"email": "a@example.com", "password": password,
                                        "csrf_token": csrf})
        assert r.status_code == 303
    store.close()
