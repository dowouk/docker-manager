"""Registry logins: host normalisation, the generated Docker config that names the
credential helper, and a live login test. No password is ever written to a file."""
import base64
import contextlib
import json
import os
import tempfile

import httpx

from app.registry import _PARAM_RE, RegistryError

HELPER_NAME = "docker-manager"
HELPER_CONFIG = "docker-manager.json"
_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "hub.docker.com"}
_HUB_DOCKER_KEY = "https://index.docker.io/v1/"


def _check_port(value: str, original: str) -> None:
    if value.startswith("["):  # [::1]:5000
        end = value.find("]")
        rest = value[end + 1:] if end != -1 else "x"
        if end == -1 or rest not in ("",) and not rest.startswith(":"):
            raise ValueError(f"Not a registry host: {original!r}")
        port = rest[1:] if rest else None
    elif value.count(":") > 1:
        raise ValueError(f"Not a registry host: {original!r}")
    else:
        port = value.partition(":")[2] if ":" in value else None
    if port is not None and not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        raise ValueError(f"Not a registry host: {original!r}")


def normalise_host(host: str) -> str:
    value = host.strip().lower()
    for scheme in ("https://", "http://"):
        value = value.removeprefix(scheme)
    value = value.rstrip("/")
    if value.removesuffix("/v1") in _HUB_ALIASES:  # Docker Hub's legacy URL
        value = value.removesuffix("/v1")
    if not value or "/" in value:
        raise ValueError(f"Not a registry host: {host!r}")
    _check_port(value, host)
    return "docker.io" if value in _HUB_ALIASES else value


def docker_host(host: str) -> str:
    """The key Docker uses for this host in config.json."""
    return _HUB_DOCKER_KEY if host == "docker.io" else host


def encode_login(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def logins_by_host(logins: list[dict]) -> dict[str, str]:
    """Normalised host -> base64 user:password, for in-memory use."""
    out: dict[str, str] = {}
    for item in logins:
        try:
            out[normalise_host(item["host"])] = encode_login(item["username"], item["password"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def auth_file_logins(path: str | None) -> dict[str, tuple[str, str]]:
    """Normalised host -> (username, password) from a Docker config's `auths`."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            auths = json.load(fh).get("auths", {})
        found: dict[str, tuple[str, str]] = {}
        for key, entry in auths.items():
            try:
                if entry.get("identitytoken"):
                    # Docker's credential-helper convention for refresh tokens
                    login = ("<token>", entry["identitytoken"])
                else:
                    user, _, password = base64.b64decode(entry["auth"], validate=True).decode().partition(":")
                    login = (user, password)
                found.setdefault(normalise_host(key), login)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return found
    except (OSError, ValueError, AttributeError):
        return {}


def _write_private(path: str, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")  # mkstemp is 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _base_config(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_docker_config(
    config_dir: str, hosts: list[str], db_path: str, key_path: str, base_file: str | None
) -> str | None:
    """Write a Docker config that routes the saved-login hosts to the credential helper,
    plus the helper's own paths-only config. None (and no files) if there are no saved
    logins: Docker then keeps behaving exactly as in 0.1.

    The base file's credHelpers and credsStore are kept as they are (they hold no
    secrets); its `auths` hosts with a login are routed to the helper, which reads the
    base file live. A base credHelpers entry is only replaced for a host with a saved login.
    """
    files = [os.path.join(config_dir, "config.json"), os.path.join(config_dir, HELPER_CONFIG)]
    ui: dict[str, None] = {}
    for host in hosts:
        try:
            ui[normalise_host(host)] = None
        except ValueError:
            continue
    if not ui:
        for path in files:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        return None
    base = _base_config(base_file)
    base_helpers = base.get("credHelpers")
    helpers: dict[str, str] = {}
    for key, value in (base_helpers if isinstance(base_helpers, dict) else {}).items():
        try:
            if normalise_host(key) in ui:
                continue  # a saved login wins
        except ValueError:
            pass
        helpers[key] = value
    taken = set()
    for key in helpers:
        with contextlib.suppress(ValueError):
            taken.add(normalise_host(key))
    for host in ui:
        helpers[docker_host(host)] = HELPER_NAME
    auths = base.get("auths")
    for key, entry in (auths.items() if isinstance(auths, dict) else ()):
        try:
            host = normalise_host(key)
        except ValueError:
            continue
        if host in ui or host in taken or not isinstance(entry, dict):
            continue
        if entry.get("auth") or entry.get("identitytoken"):
            helpers[docker_host(host)] = HELPER_NAME
    config: dict = {"credHelpers": helpers}
    if "credsStore" in base:
        config["credsStore"] = base["credsStore"]
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    _write_private(files[0], config)
    _write_private(files[1], {"db": db_path, "key": key_path, "auth_file": base_file})
    return config_dir


async def test_login(http: httpx.AsyncClient, host: str, username: str, password: str) -> None:
    """Raise RegistryError unless the registry accepts this login."""
    registry = normalise_host(host)
    api = "registry-1.docker.io" if registry == "docker.io" else registry
    url = f"https://{api}/v2/"
    basic = httpx.BasicAuth(username, password)
    refused = RegistryError("The registry refused this username or password")
    try:
        resp = await http.get(url)
        if resp.status_code == 401:
            challenge = resp.headers.get("WWW-Authenticate", "")
            if challenge.lower().startswith("bearer"):
                params = dict(_PARAM_RE.findall(challenge))
                realm = params.get("realm")
                if not realm:
                    raise RegistryError(f"{registry} sent an unusable authentication challenge")
                query = {k: v for k, v in params.items() if k in ("service", "scope")}
                resp = await http.get(realm, params=query, auth=basic)
            elif challenge.lower().startswith("basic"):
                resp = await http.get(url, auth=basic)
            else:
                raise RegistryError(f"{registry} sent an unusable authentication challenge")
            if resp.status_code in (401, 403):
                raise refused
        if not resp.is_success:
            raise RegistryError(f"{registry} returned HTTP {resp.status_code}")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise RegistryError(f"{registry} could not be reached") from exc


test_login.__test__ = False  # not a pytest test, whatever imports it
