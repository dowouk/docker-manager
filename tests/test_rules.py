import time

import pytest

from app.checker import Status, check_all
from app.inventory import ContainerInfo
from app.rules import Rules, RulesError, RulesStore, tag_allowed
from app.store import Store
from test_checker import FakeRegistry, info


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "rules.db"))


def container(**labels):
    return ContainerInfo(
        id="a" * 12, name="web", image_ref="nginx:1.2", image_id="sha256:i",
        running_repo_digests=[], local_tag_image_id=None, project="site", working_dir=None,
        config_files=[], service="web", state="running", is_self=False, labels=labels,
    )


def test_defaults(store):
    rules = RulesStore(store).effective(container())
    assert rules == Rules()


def test_label_booleans_and_lists(store):
    rules = RulesStore(store).effective(container(**{
        "docker-manager.enable": "No",
        "docker-manager.notify": "FALSE",
        "docker-manager.layout.ignore": "yes",
        "docker-manager.include_tags": "^1\\.\\d+$; 2\\..*",
        "docker-manager.exclude_tags": ".*-rc.*",
        "docker-manager.release_notes": "https://example.com/notes",
    }))
    assert not rules.watch and not rules.notify and rules.layout_ignore
    assert rules.include_tags == ("^1\\.\\d+$", "2\\..*")
    assert rules.exclude_tags == (".*-rc.*",)
    assert rules.release_notes == "https://example.com/notes"
    assert rules.warnings == ()
    assert rules.from_label == {"watch", "notify", "layout_ignore", "include_tags",
                                "exclude_tags", "release_notes"}


def test_bad_values_ignored_with_warning(store):
    rules = RulesStore(store).effective(container(**{
        "docker-manager.enable": "maybe",
        "docker-manager.release_notes": "ftp://example.com/x",
        "docker-manager.include_tags": ";".join(["a"] * 11),
        "docker-manager.exclude_tags": "x" * 201,
    }))
    assert rules.watch and rules.release_notes is None
    assert rules.include_tags == () and rules.exclude_tags == ()
    assert len(rules.warnings) == 4
    assert rules.from_label == frozenset()


def test_label_wins_over_ui(store):
    rs = RulesStore(store)
    rs.save("site/web", {"watch": "no", "notify": "no", "exclude_tags": "a"}, "me@x")
    rules = rs.effective(container(**{"docker-manager.enable": "true"}))
    assert rules.watch and "watch" in rules.from_label
    assert not rules.notify and rules.exclude_tags == ("a",)


def test_invalid_label_falls_back_to_ui_value(store):
    rs = RulesStore(store)
    rs.save("site/web", {"watch": "no"}, "me@x")
    rules = rs.effective(container(**{"docker-manager.enable": "perhaps"}))
    assert not rules.watch and rules.warnings


def test_save_validates_and_audits(store):
    rs = RulesStore(store)
    with pytest.raises(RulesError) as exc:
        rs.save("site/web", {"include_tags": "(", "release_notes": "javascript:x"}, "me@x")
    assert set(exc.value.errors) == {"include_tags", "release_notes"}
    assert rs.saved("site/web") == {}
    rs.save("site/web", {"include_tags": "1\\..*", "layout_ignore": "yes"}, "me@x")
    assert rs.saved("site/web")["include_tags"] == ["1\\..*"]
    assert any(a.summary == "Watch rules changed: site/web" and a.kind == "updates"
               for a in store.list_audit())


def test_save_keeps_absent_fields(store):
    rs = RulesStore(store)
    rs.save("k", {"watch": "no", "notify": "no"}, "u")
    rs.save("k", {"notify": "yes"}, "u")
    assert rs.saved("k") == {"watch": False, "notify": True}


def test_tag_filter_whole_tag():
    rules = Rules(include_tags=("16",))
    assert tag_allowed("16", rules) and not tag_allowed("16.2", rules)
    rules = Rules(include_tags=("16\\..*",), exclude_tags=(".*-alpine",))
    assert tag_allowed("16.2", rules) and not tag_allowed("16.2-alpine", rules)
    assert not tag_allowed("17.1", rules)
    assert tag_allowed("anything", Rules())


def test_regex_package_defuses_classic_pattern():
    # `regex` optimises (a+)+$ so it never blows up; it must still return promptly.
    started = time.monotonic()
    assert tag_allowed("a" * 40 + "!", Rules(exclude_tags=("(a+)+$",)))
    assert time.monotonic() - started < 1


def test_pathological_pattern_times_out_with_warning():
    rules = Rules(exclude_tags=("(a|aa)+$",))
    warnings: list[str] = []
    started = time.monotonic()
    assert tag_allowed("a" * 40 + "!", rules, warnings)
    assert time.monotonic() - started < 1
    assert len(warnings) == 1 and "(a|aa)+$" in warnings[0]


def test_bad_pattern_is_no_match_with_warning():
    warnings: list[str] = []
    assert tag_allowed("1", Rules(exclude_tags=("(",)), warnings)
    assert not tag_allowed("1", Rules(include_tags=("(",)), warnings)
    assert len(warnings) == 1  # the same warning is reported once


async def test_not_watched_skips_registry(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:new", "redis:7.0": "sha256:new"},
                       {"redis:7.0": ["7.1"]})
    off = info(tmp_path, name="off")
    on = info(tmp_path, name="on", image="redis:7.0", digests=["redis@sha256:old"])
    result = await check_all(
        [off, on], reg, settings, None,
        rules_for=lambda c: Rules(watch=c.name != "off"))
    assert result.containers[0].status == Status.NOT_WATCHED
    assert result.containers[1].minor_update == "7.1"
    assert reg.head_calls == ["redis:7.0"]
    assert result.summary() == {"up_to_date": 0, "updates": 1, "major": 0, "errors": 0}


async def test_shared_image_still_checked_when_one_user_is_watched(tmp_path, settings):
    reg = FakeRegistry({"nginx:latest": "sha256:new"})
    a, b = info(tmp_path, name="a"), info(tmp_path, name="b")
    result = await check_all([a, b], reg, settings, None,
                             rules_for=lambda c: Rules(watch=c.name == "b"))
    assert [cs.status for cs in result.containers] == [Status.NOT_WATCHED, Status.NEWER_BUILD]
    assert len(reg.head_calls) == 1


async def test_tag_filter_applies_per_container(tmp_path, settings):
    reg = FakeRegistry({"pg:16.2": "sha256:old"}, {"pg:16.2": ["16.3", "16.4-alpine", "17.0"]})
    c = info(tmp_path, name="db", image="pg:16.2", digests=["pg@sha256:old"])
    plain = await check_all([c], reg, settings, None)
    assert plain.containers[0].minor_update == "16.4-alpine" or plain.containers[0].minor_update == "16.3"
    result = await check_all(
        [c], reg, settings, None,
        rules_for=lambda _: Rules(exclude_tags=("16\\.3",), include_tags=("1.*",)),
    )
    cs = result.containers[0]
    assert cs.minor_update is None and cs.major_update == "17.0"
    assert cs.notify and cs.rule_warnings == []


async def test_pathological_rule_does_not_stop_check(tmp_path, settings):
    reg = FakeRegistry({"pg:16.2": "sha256:old"}, {"pg:16.2": ["16.3", "a" * 40 + "!"]})
    c = info(tmp_path, name="db", image="pg:16.2", digests=["pg@sha256:old"])
    started = time.monotonic()
    result = await check_all([c], reg, settings, None,
                             rules_for=lambda _: Rules(exclude_tags=("(a|aa)+$",)))
    assert time.monotonic() - started < 2
    assert result.containers[0].minor_update == "16.3"
    assert len(result.containers[0].rule_warnings) == 1


def test_old_stored_result_loads():
    from app.checker import CheckResult
    data = {"checked_at": "2026-10-01T00:00:00+00:00", "mailcow": None, "containers": [{
        "container": {"id": "a", "name": "w", "image_ref": "x:1", "image_id": "i",
                      "running_repo_digests": [], "local_tag_image_id": None, "project": None,
                      "working_dir": None, "config_files": [], "service": None,
                      "state": "running", "is_self": False},
        "status": "up_to_date"}]}
    cs = CheckResult.from_dict(data).containers[0]
    assert cs.notify and cs.rule_warnings == []


async def test_not_watched_and_notify_false_in_views(tmp_path, settings):
    from app import view
    from app.digest import render_digest
    from app.notify.notifier import current_updates, check_failed
    reg = FakeRegistry({"nginx:latest": "sha256:new"})
    quiet = info(tmp_path, name="quiet")
    off = info(tmp_path, name="off")
    result = await check_all(
        [quiet, off], reg, settings, None,
        rules_for=lambda c: Rules(watch=c.name != "off", notify=c.name != "quiet"))
    assert view.row_state(result.containers[1]) == "not_watched"
    assert view.row_state(result.containers[0]) == "update"  # still shown on the dashboard
    ov = view.overview(result)
    assert (ov.updates, ov.not_watched) == (1, 1)
    assert [a.name for a in view.attention(result)] == ["quiet"]
    assert current_updates(result) == {}
    assert not check_failed(result)
    subject, html, _ = render_digest(result, settings)
    assert subject.endswith("all up to date") and "quiet" not in html and "off" not in html
