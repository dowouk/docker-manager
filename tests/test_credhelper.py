import base64
import gc
import io
import json
import os

import pytest

from app.credhelper import main
from app.keys import SecretBox
from app.registry_logins import write_docker_config
from app.settings_service import SettingsService
from app.store import Store

KEY = b"k" * 32


def run(*args, stdin=""):
    out = io.StringIO()
    code = main(list(args), stdin=io.StringIO(stdin), stdout=out)
    return code, out.getvalue().strip()


@pytest.fixture
def env(settings, tmp_path, monkeypatch):
    db = str(tmp_path / "dashboard.db")
    key = tmp_path / "secret.key"
    key.write_bytes(KEY)
    svc = SettingsService(settings, Store(db), SecretBox(KEY))
    svc.save_registry_logins(
        [{"host": "ghcr.io", "username": "me", "password": "tok"},
         {"host": "docker.io", "username": "hubuser", "password": "hubpw"}], "me", "x")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auths": {"quay.io": {"auth": base64.b64encode(b"q:pw").decode()}}}))
    dc = write_docker_config(str(tmp_path / "dc"), ["ghcr.io", "docker.io"], db, str(key), str(auth))
    monkeypatch.setenv("DOCKER_CONFIG", dc)
    return tmp_path


def test_get_returns_login(env):
    code, out = run("get", stdin="ghcr.io\n")
    assert code == 0
    assert json.loads(out) == {"ServerURL": "ghcr.io", "Username": "me", "Secret": "tok"}


def test_get_docker_hub_legacy_url(env):
    code, out = run("get", stdin="https://index.docker.io/v1/")
    assert code == 0
    assert json.loads(out)["Username"] == "hubuser"
    assert json.loads(out)["Secret"] == "hubpw"


def test_get_falls_back_to_auth_file(env):
    code, out = run("get", stdin="quay.io")
    assert code == 0
    assert (json.loads(out)["Username"], json.loads(out)["Secret"]) == ("q", "pw")


def test_get_unknown_host(env):
    assert run("get", stdin="example.com") == (1, "credentials not found in native keychain")


def test_get_wrong_key_is_not_found(env, capsys):
    (env / "secret.key").write_bytes(b"z" * 32)
    assert run("get", stdin="ghcr.io") == (1, "credentials not found in native keychain")
    assert capsys.readouterr().err == ""


def test_missing_key_is_not_found_and_not_created(env):
    (env / "secret.key").unlink()
    assert run("get", stdin="ghcr.io")[0] == 1
    assert not (env / "secret.key").exists()


def test_missing_db_is_not_found_and_not_created(env):
    for f in env.glob("dashboard.db*"):
        f.unlink()
    assert run("get", stdin="ghcr.io") == (1, "credentials not found in native keychain")
    assert not (env / "dashboard.db").exists()


def test_no_config_is_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "nowhere"))
    assert run("get", stdin="ghcr.io")[0] == 1


def test_store_and_erase_are_noops(env):
    before = (env / "dashboard.db").read_bytes()
    assert run("store", stdin='{"ServerURL":"ghcr.io","Username":"a","Secret":"b"}')[0] == 0
    assert run("erase", stdin="ghcr.io")[0] == 0
    assert (env / "dashboard.db").read_bytes() == before
    assert run("get", stdin="ghcr.io")[0] == 0


def test_list_has_no_secrets(env):
    code, out = run("list")
    assert code == 0
    assert json.loads(out) == {"ghcr.io": "me", "docker.io": "hubuser", "quay.io": "q"}
    assert "tok" not in out and "hubpw" not in out


def test_get_identity_token_from_auth_file(env):
    auth = env / "auth.json"
    auth.write_text(json.dumps({"auths": {"myreg.example.com": {"identitytoken": "refresh-tok"}}}))
    code, out = run("get", stdin="myreg.example.com")
    assert code == 0
    assert (json.loads(out)["Username"], json.loads(out)["Secret"]) == ("<token>", "refresh-tok")


def test_unset_docker_config_is_not_found(env, monkeypatch):
    monkeypatch.delenv("DOCKER_CONFIG")
    monkeypatch.chdir(env / "dc")  # holds docker-manager.json, which must not be read
    assert run("get", stdin="ghcr.io")[0] == 1


def test_corrupt_db_is_not_found(env, capsys):
    gc.collect()  # a still-open connection in this process would share the old WAL index
    for f in env.glob("dashboard.db-*"):  # WAL files would still hold the rows
        f.unlink()
    (env / "dashboard.db").write_bytes(b"not a database" * 100)
    assert run("get", stdin="ghcr.io") == (1, "credentials not found in native keychain")
    assert capsys.readouterr().err == ""


def test_helper_does_not_touch_the_database(env):
    before = (env / "dashboard.db").stat().st_mtime_ns
    run("get", stdin="ghcr.io")
    assert (env / "dashboard.db").stat().st_mtime_ns == before
