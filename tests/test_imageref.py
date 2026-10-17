import pytest

from app.imageref import ImageRef, parse_image_ref

DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "ref, registry, repository, tag, digest",
    [
        ("nginx", "docker.io", "library/nginx", "latest", None),
        ("nginx:1.25-alpine", "docker.io", "library/nginx", "1.25-alpine", None),
        ("louislam/uptime-kuma:1", "docker.io", "louislam/uptime-kuma", "1", None),
        ("ghcr.io/owner/app:v2.1.0", "ghcr.io", "owner/app", "v2.1.0", None),
        ("localhost:5000/app:dev", "localhost:5000", "app", "dev", None),
        ("lscr.io/linuxserver/plex", "lscr.io", "linuxserver/plex", "latest", None),
        (f"nginx@{DIGEST}", "docker.io", "library/nginx", None, DIGEST),
        ("docker.io/library/redis:7", "docker.io", "library/redis", "7", None),
    ],
)
def test_parse(ref, registry, repository, tag, digest):
    parsed = parse_image_ref(ref)
    assert isinstance(parsed, ImageRef)
    assert (parsed.registry, parsed.repository, parsed.tag, parsed.digest) == (
        registry,
        repository,
        tag,
        digest,
    )


def test_tag_and_digest_together():
    parsed = parse_image_ref(f"ghcr.io/o/a:1@{DIGEST}")
    assert (parsed.tag, parsed.digest) == ("1", DIGEST)


def test_localhost_without_port_is_registry():
    assert parse_image_ref("localhost/app").registry == "localhost"


def test_api_host_and_is_docker_hub():
    hub = parse_image_ref("nginx")
    assert hub.api_host == "registry-1.docker.io"
    assert hub.is_docker_hub
    other = parse_image_ref("ghcr.io/o/a")
    assert other.api_host == "ghcr.io"
    assert not other.is_docker_hub


@pytest.mark.parametrize(
    "ref, new_tag, expected",
    [
        ("nginx:1.25", "1.26", "nginx:1.26"),
        ("nginx", "1.26", "nginx:1.26"),
        ("ghcr.io/o/a:1", "2", "ghcr.io/o/a:2"),
        ("docker.io/library/nginx:1.25", "1.26", "docker.io/library/nginx:1.26"),
        ("louislam/uptime-kuma:1", "2", "louislam/uptime-kuma:2"),
        ("localhost:5000/app:dev", "prod", "localhost:5000/app:prod"),
        (f"nginx@{DIGEST}", "1.26", "nginx:1.26"),
    ],
)
def test_with_tag_preserves_user_style(ref, new_tag, expected):
    assert parse_image_ref(ref).with_tag(new_tag) == expected
