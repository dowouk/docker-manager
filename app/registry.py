import asyncio
import base64
import binascii
import json
import re
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from app.imageref import DOCKER_HUB, ImageRef

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
HUB_TAGS_URL = "https://hub.docker.com/v2/repositories/{repo}/tags"
_HUB_CONFIG_KEYS = ("https://index.docker.io/v1/", "index.docker.io", "docker.io", "registry-1.docker.io")
_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')
_NEXT_LINK_RE = re.compile(r'<([^>]*)>\s*;[^,]*rel="?next"?')
# The login service Docker Hub's own registry sends clients to.
_HUB_AUTH_HOST = "auth.docker.io"
_HUB_SERVICE = "registry.docker.io"


class RegistryError(Exception):
    pass


class RateLimitError(RegistryError):
    pass


class NotFoundError(RegistryError):
    """The registry doesn't have this image (or won't say it exists): typically one
    built or loaded on the server, or a private image without credentials."""


class _UsesDockerHub(Exception):
    """A registry sent us to Docker Hub's own login: it is a front for Docker Hub."""


class RegistryClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        auth_file: str | None = None,
        tag_cache_ttl: float = 86400,
        max_tag_pages: int = 10,
        tag_failure_ttl: float = 900,
        clock: Callable[[], float] = time.monotonic,
        logins: dict[str, str] | None = None,
    ) -> None:
        self._http = http
        self._config_path = auth_file
        self._logins = logins or {}  # normalised host -> base64 user:password, memory only
        self._ttl = tag_cache_ttl
        self._max_pages = max_tag_pages
        self._failure_ttl = tag_failure_ttl
        self._clock = clock
        self._tokens: dict[tuple[str, str], str] = {}
        self._tag_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self._tag_failures: dict[tuple[str, str], tuple[float, Exception]] = {}
        self._tag_inflight: dict[tuple[str, str], asyncio.Task[list[str]]] = {}
        # Registries found to be fronts for Docker Hub (for example docker.n8n.io).
        self._hub_fronts: set[str] = set()

    # Some registries (docker.n8n.io, for one) serve Docker Hub images through their own
    # host but fetch them from Docker Hub anonymously, from a few shared addresses whose
    # Docker Hub quota is nearly always used up – so every check through them fails with
    # HTTP 429. When a registry sends us to Docker Hub's own login, the image is the same
    # repository on Docker Hub, so check it there: that uses this server's own quota, and
    # its Docker Hub login when one is configured. Docker still pulls from the registry
    # named in the compose file; only the check is redirected.
    def _resolve(self, ref: ImageRef) -> ImageRef:
        if ref.registry in self._hub_fronts:
            return ImageRef(DOCKER_HUB, ref.repository, ref.tag, ref.digest, ref.original_name)
        return ref

    async def head_digest(self, ref: ImageRef) -> str:
        resolved = self._resolve(ref)
        try:
            return await self._head_digest(resolved)
        except _UsesDockerHub:
            self._hub_fronts.add(ref.registry)
            return await self._head_digest(self._resolve(ref))

    async def _head_digest(self, ref: ImageRef) -> str:
        reference = ref.tag or ref.digest
        url = f"https://{ref.api_host}/v2/{ref.repository}/manifests/{reference}"
        resp = await self._request("HEAD", url, ref, {"Accept": MANIFEST_ACCEPT})
        digest = resp.headers.get("Docker-Content-Digest")
        if not digest:
            raise RegistryError(f"{ref.registry} returned no Docker-Content-Digest header")
        return digest

    async def list_tags(self, ref: ImageRef) -> list[str]:
        try:
            return await self._list_tags(self._resolve(ref))
        except _UsesDockerHub:
            self._hub_fronts.add(ref.registry)
            return await self._list_tags(self._resolve(ref))

    async def _list_tags(self, ref: ImageRef) -> list[str]:
        key = (ref.registry, ref.repository)
        cached = self._tag_cache.get(key)
        if cached and self._clock() - cached[0] < self._ttl:
            return list(cached[1])
        failed = self._tag_failures.get(key)
        if failed and self._clock() - failed[0] < self._failure_ttl:
            raise failed[1]
        task = self._tag_inflight.get(key)
        if task is None:
            # One fetch per repository, however many tags of it are being checked.
            task = asyncio.get_running_loop().create_task(self._load_tags(key, ref))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._tag_inflight[key] = task
        return list(await asyncio.shield(task))

    async def _load_tags(self, key: tuple[str, str], ref: ImageRef) -> list[str]:
        try:
            tags = await (self._hub_tags(ref) if ref.is_docker_hub else self._v2_tags(ref))
        except Exception as exc:
            # Remember failures briefly so a broken repository isn't re-asked every check.
            self._tag_failures[key] = (self._clock(), exc)
            raise
        else:
            self._tag_failures.pop(key, None)
            self._tag_cache[key] = (self._clock(), tags)
            return tags
        finally:
            self._tag_inflight.pop(key, None)

    async def _hub_tags(self, ref: ImageRef) -> list[str]:
        # Newest first, so the 10-page cap keeps the tags that matter. Anonymous Hub
        # requests are refused (HTTP 403) beyond page 10; treat that as the end.
        first_url = HUB_TAGS_URL.format(repo=ref.repository) + "?page_size=100&ordering=last_updated"
        url: str | None = first_url
        tags: list[str] = []
        for page in range(self._max_pages):
            if not url:
                break
            resp = await self._request("GET", url, ref, allow=(403,) if page else ())
            if resp.status_code == 403:
                break
            data = self._json(resp, ref)
            results = data.get("results")
            if isinstance(results, list):
                tags.extend(r["name"] for r in results if isinstance(r, dict) and "name" in r)
            url = self._next_url(data.get("next"), first_url, ref)
        return tags

    async def _v2_tags(self, ref: ImageRef) -> list[str]:
        first_url = f"https://{ref.api_host}/v2/{ref.repository}/tags/list?n=1000"
        url: str | None = first_url
        tags: list[str] = []
        for _ in range(self._max_pages):
            if not url:
                break
            resp = await self._request("GET", url, ref)
            tags.extend(self._json(resp, ref).get("tags") or [])
            match = _NEXT_LINK_RE.search(resp.headers.get("Link", ""))
            url = self._next_url(urljoin(url, match.group(1)) if match else None, first_url, ref)
        return tags

    @staticmethod
    def _next_url(next_url: object, first_url: str, ref: ImageRef) -> str | None:
        if not next_url or not isinstance(next_url, str):
            return None
        if urlsplit(next_url).netloc != urlsplit(first_url).netloc:
            raise RegistryError(f"{ref.registry} pagination pointed at a different host")
        return next_url

    @staticmethod
    def _json(resp: httpx.Response, ref: ImageRef) -> dict:
        try:
            data = resp.json()
        except ValueError as exc:
            raise RegistryError(f"{ref.registry} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise RegistryError(f"{ref.registry} returned unexpected JSON")
        return data

    async def _request(
        self,
        method: str,
        url: str,
        ref: ImageRef,
        headers: dict[str, str] | None = None,
        allow: tuple[int, ...] = (),
    ) -> httpx.Response:
        # Tokens are only valid for the host that issued them, so key by request host.
        key = (urlsplit(url).netloc, ref.repository)
        resp = await self._send(method, url, ref, self._with_token(headers, key))
        if resp.status_code == 401:
            challenge = resp.headers.get("WWW-Authenticate", "")
            if not challenge.lower().startswith("bearer"):
                raise RegistryError(f"{ref.registry} returned HTTP 401 (unauthorised)")
            if not ref.is_docker_hub and _is_hub_challenge(challenge):
                raise _UsesDockerHub()
            self._tokens[key] = await self._fetch_token(challenge, ref)
            resp = await self._send(method, url, ref, self._with_token(headers, key))
        if resp.status_code == 429:
            name = "Docker Hub" if ref.is_docker_hub else ref.registry
            raise RateLimitError(f"{name} rate limit reached – try again later")
        if resp.status_code not in allow:
            # Docker Hub answers 401 even with a valid token when the repository
            # doesn't exist (or is private and we have no access); others answer 404.
            if resp.status_code == 404 or (resp.status_code == 401 and ref.is_docker_hub):
                name = "Docker Hub" if ref.is_docker_hub else ref.registry
                raise NotFoundError(f"Not found on {name} – a local or private image can't be checked")
        if not resp.is_success and resp.status_code not in allow:
            raise RegistryError(f"{ref.registry} returned HTTP {resp.status_code} for {url}")
        return resp

    def _with_token(self, headers: dict[str, str] | None, key: tuple[str, str]) -> dict[str, str]:
        out = dict(headers or {})
        if key in self._tokens:
            out["Authorization"] = f"Bearer {self._tokens[key]}"
        return out

    async def _send(self, method: str, url: str, ref: ImageRef, headers: dict[str, str]) -> httpx.Response:
        try:
            return await self._http.request(method, url, headers=headers)
        except httpx.HTTPError as exc:
            raise RegistryError(f"{ref.registry} request failed: {type(exc).__name__} {exc}".strip()) from exc

    async def _fetch_token(self, challenge: str, ref: ImageRef) -> str:
        params = dict(_PARAM_RE.findall(challenge))
        realm = params.pop("realm", None)
        if not realm:
            raise RegistryError(f"{ref.registry} sent an unusable authentication challenge")
        query = {k: v for k, v in params.items() if k in ("service", "scope")}
        headers = {}
        creds = self._credentials(ref)
        if creds:
            headers["Authorization"] = f"Basic {creds}"
        try:
            resp = await self._http.get(realm, params=query, headers=headers)
        except httpx.HTTPError as exc:
            raise RegistryError(f"{ref.registry} token request failed: {type(exc).__name__} {exc}".strip()) from exc
        if resp.status_code == 429:
            name = "Docker Hub" if ref.is_docker_hub else ref.registry
            raise RateLimitError(f"{name} rate limit reached – try again later")
        if not resp.is_success:
            raise RegistryError(f"{ref.registry} token request returned HTTP {resp.status_code}")
        data = self._json(resp, ref)
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RegistryError(f"{ref.registry} token response contained no token")
        return token

    def _credentials(self, ref: ImageRef) -> str | None:
        login = self._logins.get("docker.io" if ref.is_docker_hub else ref.registry.lower())
        if login:
            return login
        if not self._config_path:
            return None
        try:
            with open(self._config_path, encoding="utf-8") as fh:
                auths = json.load(fh).get("auths", {})
            keys = _HUB_CONFIG_KEYS if ref.is_docker_hub else (ref.registry, f"https://{ref.registry}")
            for k in keys:
                value = auths.get(k, {}).get("auth")
                if value:
                    base64.b64decode(value, validate=True)
                    return value
        except (OSError, ValueError, AttributeError, binascii.Error):
            return None
        return None


def _is_hub_challenge(challenge: str) -> bool:
    params = dict(_PARAM_RE.findall(challenge))
    return (
        params.get("service") == _HUB_SERVICE
        and urlsplit(params.get("realm", "")).netloc.lower() == _HUB_AUTH_HOST
    )
