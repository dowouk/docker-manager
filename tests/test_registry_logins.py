import json
import stat
from pathlib import Path

import httpx
import pytest

from app.registry import RegistryError
from app.registry_logins import normalise_host, test_login, write_docker_config


@pytest.mark.parametrize("raw,expected", [("https://GHCR.io/", "ghcr.io"), ("index.docker.io", "docker.io"),
                                           ("https://index.docker.io/v1/", "docker.io"),
                                           ("registry-1.docker.io", "docker.io"), ("hub.docker.com", "docker.io"),
                                           ("registry.example.com:5000", "registry.example.com:5000")])
def test_normalise_host(raw, expected):
    assert normalise_host(raw) == expected


def test_normalise_host_rejects():
    for bad in ("", "ghcr.io/owner"):
        with pytest.raises(ValueError):
            normalise_host(bad)


def test_config_names_helper_and_holds_no_secret(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"auths": {"quay.io": {"auth": "cTpwYXNzd29yZA=="}}}))
    out = write_docker_config(str(tmp_path / "dc"), ["ghcr.io", "docker.io"],
                              "/data/dashboard.db", "/data/secret.key", str(base))
    cfg = json.loads((Path(out) / "config.json").read_text())
    assert cfg == {"credHelpers": {"ghcr.io": "docker-manager", "https://index.docker.io/v1/": "docker-manager",
                                   "quay.io": "docker-manager"}}
    assert "auths" not in cfg
    assert json.loads((Path(out) / "docker-manager.json").read_text()) == {
        "db": "/data/dashboard.db", "key": "/data/secret.key", "auth_file": str(base)}
    for name in ("config.json", "docker-manager.json"):
        assert stat.S_IMODE((Path(out) / name).stat().st_mode) == 0o600


def test_no_hosts_removes_files(tmp_path):
    d = tmp_path / "dc"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "docker-manager.json").write_text("{}")
    assert write_docker_config(str(d), [], "db", "key", None) is None
    assert not (d / "config.json").exists() and not (d / "docker-manager.json").exists()


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:x:pull"'


async def test_login_bearer_ok(respx_mock, http):
    respx_mock.get("https://ghcr.io/v2/").mock(
        return_value=httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE}))
    token = respx_mock.get("https://ghcr.io/token").mock(return_value=httpx.Response(200, json={"token": "t"}))
    await test_login(http, "ghcr.io", "me", "tok")
    req = token.calls[0].request
    assert req.headers["Authorization"].startswith("Basic ")
    assert req.url.params["service"] == "ghcr.io"


async def test_login_bearer_refused(respx_mock, http):
    respx_mock.get("https://ghcr.io/v2/").mock(
        return_value=httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE}))
    respx_mock.get("https://ghcr.io/token").mock(return_value=httpx.Response(401))
    with pytest.raises(RegistryError, match="refused"):
        await test_login(http, "ghcr.io", "me", "bad")


async def test_login_basic_ok(respx_mock, http):
    def v2(request):
        if request.headers.get("Authorization", "").startswith("Basic "):
            return httpx.Response(200)
        return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="r"'})

    respx_mock.get("https://reg.example.com/v2/").mock(side_effect=v2)
    await test_login(http, "reg.example.com", "me", "tok")


async def test_login_docker_hub_uses_api_host(respx_mock, http):
    route = respx_mock.get("https://registry-1.docker.io/v2/").mock(return_value=httpx.Response(200))
    await test_login(http, "index.docker.io", "me", "tok")
    assert route.called


async def test_login_unreachable(respx_mock, http):
    respx_mock.get("https://ghcr.io/v2/").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RegistryError, match="ghcr.io could not be reached"):
        await test_login(http, "ghcr.io", "me", "tok")


def test_hub_legacy_suffix_only_for_docker_hub():
    assert normalise_host("https://index.docker.io/v1/") == "docker.io"
    with pytest.raises(ValueError):
        normalise_host("ghcr.io/v1")


def test_base_file_without_saved_logins_gives_no_config(tmp_path):
    base = tmp_path / "config.json"
    base.write_text(json.dumps({"auths": {"quay.io": {"auth": "cTpwYXNzd29yZA=="}}}))
    assert write_docker_config(str(tmp_path / "dc"), [], "db", "key", str(base)) is None
    assert not (tmp_path / "dc" / "config.json").exists()


def test_base_file_kept_alongside_saved_login(tmp_path):
    base = tmp_path / "config.json"
    base.write_text(json.dumps({
        "auths": {"quay.io": {"auth": "cTpwYXNzd29yZA=="},
                  "myreg.example.com": {"identitytoken": "tok"},
                  "empty.example.com": {}},
        "credHelpers": {"123.dkr.ecr.eu-west-2.amazonaws.com": "ecr-login", "ghcr.io": "other"},
        "credsStore": "desktop"}))
    out = write_docker_config(str(tmp_path / "dc"), ["ghcr.io"], "db", "key", str(base))
    assert json.loads((Path(out) / "config.json").read_text()) == {
        "credHelpers": {"123.dkr.ecr.eu-west-2.amazonaws.com": "ecr-login", "ghcr.io": "docker-manager",
                        "quay.io": "docker-manager", "myreg.example.com": "docker-manager"},
        "credsStore": "desktop"}
