import base64
import json

import httpx
import pytest

from app.imageref import parse_image_ref
from app.registry import NotFoundError, RateLimitError, RegistryClient, RegistryError

MANIFEST = "https://registry-1.docker.io/v2/library/nginx/manifests/1.25"
TOKEN = "https://auth.docker.io/token"
CHALLENGE = (
    'Bearer realm="https://auth.docker.io/token",'
    'service="registry.docker.io",scope="repository:library/nginx:pull"'
)


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


async def test_head_digest_with_bearer_auth(respx_mock, http):
    def manifest(request):
        if request.headers.get("Authorization") == "Bearer t":
            return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:aaa"})
        return httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE})

    route = respx_mock.head(MANIFEST).mock(side_effect=manifest)
    token = respx_mock.get(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    client = RegistryClient(http)
    assert await client.head_digest(parse_image_ref("nginx:1.25")) == "sha256:aaa"
    assert route.call_count == 2
    req = token.calls[0].request
    assert req.url.params["service"] == "registry.docker.io"
    assert req.url.params["scope"] == "repository:library/nginx:pull"
    assert "Authorization" not in req.headers
    accept = route.calls[0].request.headers["Accept"]
    assert "application/vnd.oci.image.index.v1+json" in accept
    assert "application/vnd.docker.distribution.manifest.list.v2+json" in accept
    assert all(c.request.method == "HEAD" for c in respx_mock.calls if "manifests" in str(c.request.url))
    # token is cached: second call needs no new token request
    await client.head_digest(parse_image_ref("nginx:1.25"))
    assert token.call_count == 1


async def test_rate_limit_raises(respx_mock, http):
    respx_mock.head(MANIFEST).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError, match="Docker Hub rate limit reached"):
        await RegistryClient(http).head_digest(parse_image_ref("nginx:1.25"))


async def test_rate_limit_message_other_registry(respx_mock, http):
    respx_mock.head("https://ghcr.io/v2/o/app/manifests/1").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError, match="ghcr.io rate limit reached – try again later"):
        await RegistryClient(http).head_digest(parse_image_ref("ghcr.io/o/app:1"))


async def test_server_error_raises_registry_error(respx_mock, http):
    respx_mock.head(MANIFEST).mock(return_value=httpx.Response(500))
    with pytest.raises(RegistryError, match="500"):
        await RegistryClient(http).head_digest(parse_image_ref("nginx:1.25"))


async def test_ghcr_tags_follow_link_header(respx_mock, http):
    def page(request):
        if request.url.params.get("last") == "1.1":
            return httpx.Response(200, json={"tags": ["1.2"]})
        return httpx.Response(
            200,
            json={"tags": ["1.0", "1.1"]},
            headers={"Link": '</v2/o/app/tags/list?n=1000&last=1.1>; rel="next"'},
        )

    respx_mock.get("https://ghcr.io/v2/o/app/tags/list").mock(side_effect=page)
    tags = await RegistryClient(http).list_tags(parse_image_ref("ghcr.io/o/app:1.0"))
    assert tags == ["1.0", "1.1", "1.2"]


async def test_hub_tags_use_hub_api_and_page_cap(respx_mock, http):
    base = "https://hub.docker.com/v2/repositories/library/nginx/tags"
    p1 = respx_mock.get(base, params={"page_size": "100", "ordering": "last_updated"}).mock(
        return_value=httpx.Response(200, json={"next": base + "?page=2", "results": [{"name": "a"}]})
    )
    p2 = respx_mock.get(base, params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"next": base + "?page=3", "results": [{"name": "b"}]})
    )
    p3 = respx_mock.get(base, params={"page": "3"}).mock(
        return_value=httpx.Response(200, json={"next": None, "results": [{"name": "c"}]})
    )
    tags = await RegistryClient(http, max_tag_pages=2).list_tags(parse_image_ref("nginx"))
    assert tags == ["a", "b"]
    assert (p1.call_count, p2.call_count, p3.call_count) == (1, 1, 0)


async def test_tag_list_cached(respx_mock, http):
    route = respx_mock.get("https://ghcr.io/v2/o/app/tags/list").mock(
        return_value=httpx.Response(200, json={"tags": ["1"]})
    )
    now = [0.0]
    client = RegistryClient(http, tag_cache_ttl=100, clock=lambda: now[0])
    ref = parse_image_ref("ghcr.io/o/app")
    await client.list_tags(ref)
    now[0] = 99
    await client.list_tags(ref)
    assert route.call_count == 1
    now[0] = 101
    await client.list_tags(ref)
    assert route.call_count == 2


async def test_timeout_raises_registry_error(respx_mock, http):
    respx_mock.head(MANIFEST).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(RegistryError):
        await RegistryClient(http).head_digest(parse_image_ref("nginx:1.25"))


async def test_registry_auth_file_credentials_used(respx_mock, http, tmp_path):
    cfg = tmp_path / "config.json"
    auth = base64.b64encode(b"user:pass").decode()
    cfg.write_text(json.dumps({"auths": {"https://index.docker.io/v1/": {"auth": auth}}}))
    respx_mock.head(MANIFEST).mock(
        side_effect=lambda r: httpx.Response(200, headers={"Docker-Content-Digest": "sha256:b"})
        if r.headers.get("Authorization") == "Bearer t"
        else httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE})
    )
    token = respx_mock.get(TOKEN).mock(return_value=httpx.Response(200, json={"token": "t"}))
    client = RegistryClient(http, auth_file=str(cfg))
    assert await client.head_digest(parse_image_ref("nginx:1.25")) == "sha256:b"
    assert token.calls[0].request.headers["Authorization"] == f"Basic {auth}"


async def test_missing_config_means_no_credentials(respx_mock, http, tmp_path):
    respx_mock.head(MANIFEST).mock(
        side_effect=lambda r: httpx.Response(200, headers={"Docker-Content-Digest": "sha256:c"})
        if r.headers.get("Authorization") == "Bearer t"
        else httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE})
    )
    token = respx_mock.get(TOKEN).mock(return_value=httpx.Response(200, json={"token": "t"}))
    client = RegistryClient(http, auth_file=str(tmp_path / "nope.json"))
    assert await client.head_digest(parse_image_ref("nginx:1.25")) == "sha256:c"
    assert "Authorization" not in token.calls[0].request.headers


async def test_token_not_sent_to_other_host(respx_mock, http):
    respx_mock.head(MANIFEST).mock(
        side_effect=lambda r: httpx.Response(200, headers={"Docker-Content-Digest": "sha256:a"})
        if r.headers.get("Authorization") == "Bearer t"
        else httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE})
    )
    respx_mock.get(TOKEN).mock(return_value=httpx.Response(200, json={"token": "t"}))
    hub = respx_mock.get("https://hub.docker.com/v2/repositories/library/nginx/tags").mock(
        return_value=httpx.Response(200, json={"next": None, "results": [{"name": "a"}]})
    )
    client = RegistryClient(http)
    ref = parse_image_ref("nginx:1.25")
    await client.head_digest(ref)
    assert await client.list_tags(ref) == ["a"]
    assert "Authorization" not in hub.calls[0].request.headers


async def test_next_page_on_other_host_rejected(respx_mock, http):
    respx_mock.get("https://ghcr.io/v2/o/app/tags/list").mock(
        return_value=httpx.Response(
            200, json={"tags": ["1"]}, headers={"Link": '<https://evil.example/x>; rel="next"'}
        )
    )
    evil = respx_mock.get("https://evil.example/x").mock(return_value=httpx.Response(200, json={"tags": []}))
    with pytest.raises(RegistryError, match="different host"):
        await RegistryClient(http).list_tags(parse_image_ref("ghcr.io/o/app"))
    assert evil.call_count == 0


async def test_hub_non_dict_results_skipped(respx_mock, http):
    respx_mock.get("https://hub.docker.com/v2/repositories/library/nginx/tags").mock(
        return_value=httpx.Response(200, json={"next": None, "results": ["x", None, {"name": "a"}]})
    )
    assert await RegistryClient(http).list_tags(parse_image_ref("nginx")) == ["a"]


async def test_double_401_retries_once(respx_mock, http):
    route = respx_mock.head(MANIFEST).mock(
        return_value=httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE})
    )
    respx_mock.get(TOKEN).mock(return_value=httpx.Response(200, json={"token": "t"}))
    # Docker Hub refuses a valid token like this when the repository doesn't exist
    with pytest.raises(NotFoundError, match="Not found on Docker Hub"):
        await RegistryClient(http).head_digest(parse_image_ref("nginx:1.25"))
    assert route.call_count == 2


async def test_double_401_elsewhere_is_an_auth_error(respx_mock, http):
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:o/app:pull"'
    route = respx_mock.head("https://ghcr.io/v2/o/app/manifests/1").mock(
        return_value=httpx.Response(401, headers={"WWW-Authenticate": challenge}))
    respx_mock.get("https://ghcr.io/token").mock(return_value=httpx.Response(200, json={"token": "t"}))
    with pytest.raises(RegistryError, match="401") as exc:
        await RegistryClient(http).head_digest(parse_image_ref("ghcr.io/o/app:1"))
    assert not isinstance(exc.value, NotFoundError)
    assert route.call_count == 2


async def test_404_is_not_found(respx_mock, http):
    respx_mock.head("https://ghcr.io/v2/o/gone/manifests/1").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFoundError, match="Not found on ghcr.io – a local or private image can't be checked"):
        await RegistryClient(http).head_digest(parse_image_ref("ghcr.io/o/gone:1"))


async def test_token_endpoint_429(respx_mock, http):
    respx_mock.head(MANIFEST).mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE}))
    respx_mock.get(TOKEN).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError, match="Docker Hub rate limit reached"):
        await RegistryClient(http).head_digest(parse_image_ref("nginx:1.25"))


HUB = "https://hub.docker.com/v2/repositories/library/nginx/tags"


def hub_pages(respx_mock, pages: int, forbidden_from: int):
    """Hub tag pages 1..pages-1 each link to the next; page >= forbidden_from returns 403."""
    def page(request):
        n = int(request.url.params.get("page", "1"))
        if n >= forbidden_from:
            return httpx.Response(403, json={"message": "pagination offset too large for anonymous requests"})
        nxt = f"{HUB}?page={n + 1}" if n < pages else None
        return httpx.Response(200, json={"next": nxt, "results": [{"name": f"t{n}"}]})

    return respx_mock.get(HUB).mock(side_effect=page)


async def test_hub_tags_default_cap_is_ten_pages(respx_mock, http):
    route = hub_pages(respx_mock, pages=50, forbidden_from=11)
    tags = await RegistryClient(http).list_tags(parse_image_ref("nginx"))
    assert tags == [f"t{n}" for n in range(1, 11)]
    assert route.call_count == 10
    first = route.calls[0].request.url.params
    assert first["page_size"] == "100" and first["ordering"] == "last_updated"


async def test_hub_403_after_first_page_keeps_collected_tags_and_caches(respx_mock, http):
    route = hub_pages(respx_mock, pages=50, forbidden_from=3)
    client = RegistryClient(http, max_tag_pages=20)
    ref = parse_image_ref("nginx")
    assert await client.list_tags(ref) == ["t1", "t2"]
    assert await client.list_tags(ref) == ["t1", "t2"]
    assert route.call_count == 3


async def test_hub_403_on_first_page_is_an_error(respx_mock, http):
    hub_pages(respx_mock, pages=5, forbidden_from=1)
    with pytest.raises(RegistryError, match="403"):
        await RegistryClient(http).list_tags(parse_image_ref("nginx"))


async def test_concurrent_list_tags_share_one_fetch(respx_mock, http):
    import asyncio

    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"tags": ["1", "2"]})

    route = respx_mock.get("https://ghcr.io/v2/o/app/tags/list").mock(side_effect=slow)
    client = RegistryClient(http)
    a, b = await asyncio.gather(
        client.list_tags(parse_image_ref("ghcr.io/o/app:1")),
        client.list_tags(parse_image_ref("ghcr.io/o/app:2")),
    )
    assert a == b == ["1", "2"]
    assert route.call_count == 1


async def test_tag_list_failure_cached_briefly(respx_mock, http):
    route = respx_mock.get("https://ghcr.io/v2/o/app/tags/list").mock(return_value=httpx.Response(500))
    now = [0.0]
    client = RegistryClient(http, tag_failure_ttl=900, clock=lambda: now[0])
    ref = parse_image_ref("ghcr.io/o/app")
    for t in (0, 899):
        now[0] = t
        with pytest.raises(RegistryError, match="500"):
            await client.list_tags(ref)
    assert route.call_count == 1
    now[0] = 901
    with pytest.raises(RegistryError):
        await client.list_tags(ref)
    assert route.call_count == 2


# ---- Registries that are fronts for Docker Hub (docker.n8n.io) ----------------------

N8N_MANIFEST = "https://docker.n8n.io/v2/n8nio/n8n/manifests/1.2.3"
HUB_N8N_MANIFEST = "https://registry-1.docker.io/v2/n8nio/n8n/manifests/1.2.3"
N8N_CHALLENGE = (
    'Bearer realm="https://auth.docker.io/token",'
    'service="registry.docker.io",scope="repository:n8nio/n8n:pull"'
)


def _hub_n8n(respx_mock):
    def manifest(request):
        if request.headers.get("Authorization") == "Bearer hub":
            return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:n8n"})
        return httpx.Response(401, headers={"WWW-Authenticate": N8N_CHALLENGE})

    return respx_mock.head(HUB_N8N_MANIFEST).mock(side_effect=manifest)


async def test_docker_hub_front_is_checked_on_docker_hub(respx_mock, http):
    front = respx_mock.head(N8N_MANIFEST).mock(
        return_value=httpx.Response(401, headers={"WWW-Authenticate": N8N_CHALLENGE}))
    hub = _hub_n8n(respx_mock)
    token = respx_mock.get(TOKEN).mock(return_value=httpx.Response(200, json={"token": "hub"}))
    client = RegistryClient(http)
    ref = parse_image_ref("docker.n8n.io/n8nio/n8n:1.2.3")
    assert await client.head_digest(ref) == "sha256:n8n"
    assert front.call_count == 1 and hub.call_count == 2 and token.call_count == 1
    # remembered: later checks go straight to Docker Hub
    assert await client.head_digest(ref) == "sha256:n8n"
    assert front.call_count == 1


async def test_docker_hub_front_rate_limit_never_reached(respx_mock, http):
    """The front's shared quota is used up; the check must not depend on it."""
    def front(request):
        if "Authorization" in request.headers:
            return httpx.Response(429)
        return httpx.Response(401, headers={"WWW-Authenticate": N8N_CHALLENGE})

    front_route = respx_mock.head(N8N_MANIFEST).mock(side_effect=front)
    _hub_n8n(respx_mock)
    respx_mock.get(TOKEN).mock(return_value=httpx.Response(200, json={"token": "hub"}))
    assert await RegistryClient(http).head_digest(parse_image_ref("docker.n8n.io/n8nio/n8n:1.2.3")) == "sha256:n8n"
    assert all("Authorization" not in c.request.headers for c in front_route.calls)


async def test_docker_hub_front_uses_docker_hub_login(respx_mock, http, tmp_path):
    auth = base64.b64encode(b"me:pat").decode()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auths": {"https://index.docker.io/v1/": {"auth": auth}}}))
    respx_mock.head(N8N_MANIFEST).mock(
        return_value=httpx.Response(401, headers={"WWW-Authenticate": N8N_CHALLENGE}))
    _hub_n8n(respx_mock)
    token = respx_mock.get(TOKEN).mock(return_value=httpx.Response(200, json={"token": "hub"}))
    await RegistryClient(http, auth_file=str(cfg)).head_digest(parse_image_ref("docker.n8n.io/n8nio/n8n:1.2.3"))
    assert token.calls[0].request.headers["Authorization"] == f"Basic {auth}"


async def test_docker_hub_front_tags_come_from_docker_hub(respx_mock, http):
    respx_mock.get("https://docker.n8n.io/v2/n8nio/n8n/tags/list?n=1000").mock(
        return_value=httpx.Response(401, headers={"WWW-Authenticate": N8N_CHALLENGE}))
    hub = respx_mock.get(url__startswith="https://hub.docker.com/v2/repositories/n8nio/n8n/tags").mock(
        return_value=httpx.Response(200, json={"results": [{"name": "1.2.4"}], "next": None}))
    tags = await RegistryClient(http).list_tags(parse_image_ref("docker.n8n.io/n8nio/n8n:1.2.3"))
    assert tags == ["1.2.4"] and hub.call_count == 1


@pytest.mark.parametrize("challenge", [
    'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:o/app:pull"',
    # Docker Hub's service name but someone else's login host: not trusted as Docker Hub
    'Bearer realm="https://auth.docker.io.evil.example/token",service="registry.docker.io"',
])
async def test_other_registries_keep_their_own_login(respx_mock, http, challenge):
    realm = challenge.split('"')[1]

    def manifest(request):
        if request.headers.get("Authorization") == "Bearer own":
            return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:own"})
        return httpx.Response(401, headers={"WWW-Authenticate": challenge})

    respx_mock.head("https://ghcr.io/v2/o/app/manifests/1").mock(side_effect=manifest)
    token = respx_mock.get(url__startswith=realm).mock(return_value=httpx.Response(200, json={"token": "own"}))
    assert await RegistryClient(http).head_digest(parse_image_ref("ghcr.io/o/app:1")) == "sha256:own"
    assert token.call_count == 1


async def test_in_memory_login_used_before_file(respx_mock, http, tmp_path):
    def b64(s):
        return base64.b64encode(s.encode()).decode()

    auth = tmp_path / "config.json"
    auth.write_text(json.dumps({"auths": {"ghcr.io": {"auth": b64("file:pw")}}}))
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:o/app:pull"'
    respx_mock.head("https://ghcr.io/v2/o/app/manifests/1").mock(side_effect=lambda r: (
        httpx.Response(200, headers={"Docker-Content-Digest": "sha256:aaa"})
        if r.headers.get("Authorization") == "Bearer t"
        else httpx.Response(401, headers={"WWW-Authenticate": challenge})))
    token = respx_mock.get("https://ghcr.io/token").mock(return_value=httpx.Response(200, json={"token": "t"}))
    client = RegistryClient(http, str(auth), logins={"ghcr.io": b64("me:tok")})
    await client.head_digest(parse_image_ref("ghcr.io/o/app:1"))
    assert token.calls[0].request.headers["Authorization"] == f"Basic {b64('me:tok')}"
