import pytest

from app.versions import ParsedTag, VersionUpdates, find_version_updates, parse_version_tag


@pytest.mark.parametrize("current,tags,minor,major", [
    ("16.2", ["16.3", "16.4", "17.0", "17.0-alpine", "16.5-rc1", "16", "16.10"], "16.10", "17.0"),
    ("1.25.4-alpine", ["1.25.5-alpine", "1.26.0-alpine", "1.25.5", "2.0.0-alpine", "1.27.0-alpine-beta"], "1.26.0-alpine", "2.0.0-alpine"),
    ("v2.11.3", ["v2.11.4", "2.12.0"], "v2.11.4", None),
    ("16", ["17", "16", "15", "20240101"], None, "17"),
    ("8.0-bookworm", ["8.1-bookworm", "8.1-bullseye", "9.0-BETA-bookworm"], "8.1-bookworm", None),
    ("3.2.1", ["3.2.0", "3.2.1"], None, None),
    ("1.0.0", ["1.0.1-RC1", "1.0.1-Preview", "2.0.0-nightly"], None, None),
    ("1.0.0", ["1.0.1-devel", "1.0.1-snapshot", "1.0.1-alpha2"], None, None),
    ("9.9", ["10.0"], None, "10.0"),
    ("1.2", ["1.2.3", "1.3.0"], None, None),
    ("1.0-source", ["1.1-source", "1.1-bookworm"], "1.1-source", None),
    ("1.0-percona", ["1.1-percona"], "1.1-percona", None),
    ("1.0-dev", ["1.1-dev", "1.1"], "1.1-dev", None),
    ("1.0-rc1", ["1.1-rc1"], "1.1-rc1", None),
    ("1.0.0", ["1.0.1rc1"], None, None),
])
def test_find_version_updates(current, tags, minor, major):
    assert find_version_updates(current, tags) == VersionUpdates(minor, major)


@pytest.mark.parametrize("tag", ["latest", "stable", "lts", "mainline", "alpine"])
def test_non_version_tags(tag):
    assert parse_version_tag(tag) is None


def test_parse_version_tag():
    assert parse_version_tag("v1.25.4-alpine") == ParsedTag("v", (1, 25, 4), "-alpine")
    assert parse_version_tag("16") == ParsedTag("", (16,), "")


def test_current_non_version_has_no_updates():
    assert find_version_updates("latest", ["1.0", "2.0"]) == VersionUpdates(None, None)
