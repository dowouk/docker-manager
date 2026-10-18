import pytest

from app.inventory import ContainerInfo
from app.links import release_notes_url
from app.rules import Rules


def make(image="nginx:latest", source=None):
    return ContainerInfo(
        id="a" * 64, name="web", image_ref=image, image_id="sha256:img",
        running_repo_digests=[], local_tag_image_id=None, project="site", working_dir=None,
        config_files=[], service="web", state="running", is_self=False, image_source=source,
    )


def test_rule_wins_over_source_and_hub():
    rules = Rules(release_notes="https://example.com/notes")
    assert release_notes_url(make(source="https://github.com/o/r"), rules) == "https://example.com/notes"


def test_http_rule_allowed():
    assert release_notes_url(make(), Rules(release_notes="http://example.com/n")) == "http://example.com/n"


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,x", "ftp://example.com/x",
                                 "https://", "https://exa mple.com", "//example.com"])
def test_unsafe_rule_is_rejected(bad):
    url = release_notes_url(make("ghcr.io/o/app:1"), Rules(release_notes=bad))
    assert url is None


def test_github_source_gives_releases():
    info = make("ghcr.io/o/app:1", source="https://github.com/owner/repo")
    assert release_notes_url(info, Rules()) == "https://github.com/owner/repo/releases"
    info = make("ghcr.io/o/app:1", source="https://github.com/owner/repo/")
    assert release_notes_url(info, Rules()) == "https://github.com/owner/repo/releases"


@pytest.mark.parametrize("source", [
    "https://gitlab.com/owner/repo", "http://github.com/owner/repo",
    "https://github.com/owner/repo/tree/main", "https://github.com.evil.example/o/r",
    "https://github.com/owner", "javascript:alert(1)",
])
def test_other_sources_ignored(source):
    assert release_notes_url(make("ghcr.io/o/app:1", source=source), Rules()) is None


def test_hub_library_image():
    assert release_notes_url(make("nginx:1.25"), Rules()) == "https://hub.docker.com/_/nginx/tags"
    assert release_notes_url(make("docker.io/library/redis"), Rules()) == "https://hub.docker.com/_/redis/tags"


def test_hub_namespaced_image():
    assert release_notes_url(make("grafana/grafana:11"), Rules()) == "https://hub.docker.com/r/grafana/grafana/tags"


def test_other_registry_without_label_has_no_link():
    assert release_notes_url(make("ghcr.io/o/app:1"), Rules()) is None
    assert release_notes_url(make("registry.example.com:5000/app"), Rules()) is None


def test_unparseable_image_has_no_link():
    assert release_notes_url(make(""), Rules()) is None
