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
    assert plain.containers[0].minor_update == "16.3"
    result = await check_all(
        [c], reg, settings, None,
        rules_for=lambda _: Rules(exclude_tags=("16\\.3",), include_tags=("1.*",)),
    )
    cs = result.containers[0]
    assert cs.minor_update is None and cs.major_update == "17.0"
    assert cs.notify and cs.rule_warnings == []


async def test_pathological_rule_does_not_stop_check(tmp_path, settings):
    reg = FakeRegistry({"pg:16.2": "sha256:old"}, {"pg:16.2": ["16.3", "1" * 40 + "!"]})
    c = info(tmp_path, name="db", image="pg:16.2", digests=["pg@sha256:old"])
    started = time.monotonic()
    result = await check_all([c], reg, settings, None,
                             rules_for=lambda _: Rules(exclude_tags=("(1|11)+$",)))
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


PROBES = ["((((a{50}){50}){50}){50})", "((((a{100}){100}){100}){100})", "(a+)+", "(a{2}){3}",
          "a{21}", "a{1,21}", "(a)\\1", "(?=a)", "(?<!a)b", "(?P=x)", "a{2}{3}", "(?(1)a|b)",
          "(?x)(?:(?:(?:(?:a{20}) {20}) {20}) {20}) {20}",
          "(?x)(((((a{20}) {20}) {20}) {20}) {20}) {20}",
          "(?x)#[\n((((a{20}){20}){20}){20}){20}\n]",
          "(?xi)(?:(?:a{20}) {20})", "(?x:(?:a{20}) {20})", "(?R)", "(a{20}){,}", "(?:abc){e<=3}",
          "a{,}", "a{3,}", "a{,3}", "a{b", "(?#c)a", "(?>a)", "(?i)a", "(?P<n>a)", "(?<n>a)"]
GOOD = ["16\\..*", "^v?\\d+$", "(a|aa)+$", "(?:1|2)\\.\\d+", "a{20}", "[a-z]{1,20}", "x{2}y{3}",
        "[(]{2}", "a+?b*", "\\{3\\}", "(?:-alpine)?", "16\\.(?:1|2)?", "\\d+(?:-alpine)?",
        "(?:a){20}", "(a|b)+", "[{]x"]


@pytest.mark.parametrize("pattern", PROBES)
def test_dangerous_patterns_rejected_before_compiling(pattern, store, monkeypatch):
    import regex
    def boom(*a, **k):
        raise AssertionError("compiled")
    monkeypatch.setattr(regex, "compile", boom)
    monkeypatch.setattr(regex, "fullmatch", boom)
    with pytest.raises(RulesError) as exc:
        RulesStore(store).save("k", {"include_tags": pattern}, "u")
    assert "include_tags" in exc.value.errors
    rules = RulesStore(store).effective(container(**{
        "docker-manager.exclude_tags": f"keep;{pattern}"}))
    assert rules.exclude_tags == ("keep",)
    assert len(rules.warnings) == 1 and "ignored" in rules.warnings[0]


@pytest.mark.parametrize("pattern", GOOD)
def test_ordinary_patterns_accepted(pattern, store):
    RulesStore(store).save("k", {"include_tags": pattern}, "u")


async def test_many_slow_patterns_do_not_stall_the_loop(tmp_path, settings):
    import asyncio
    tags = ["1" * 30 + str(i) + "!" for i in range(20)]
    slow = tuple(f"(1|11)+{i}*$" for i in range(10))
    reg = FakeRegistry({"pg:1.2": "sha256:old"}, {"pg:1.2": tags})
    cs = [info(tmp_path, name=f"db{i}", image="pg:1.2", digests=["pg@sha256:old"])
          for i in range(2)]
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    result = await check_all(cs, reg, settings, None,
                             rules_for=lambda _: Rules(exclude_tags=slow))
    elapsed = time.monotonic() - started
    task.cancel()
    assert elapsed < 2
    assert ticks > 20
    assert all(c.rule_warnings for c in result.containers)


def test_only_version_like_tags_are_matched():
    from app.rules import TagFilter
    f = TagFilter(Rules(exclude_tags=("(a|aa)+$",)))
    assert f.filter(["latest", "a" * 40 + "!", "1.2"]) == ["1.2"]
    assert f.warnings == []


def test_pattern_that_times_out_is_switched_off(monkeypatch):
    import regex
    from app.rules import TagFilter
    calls = []
    real = regex.fullmatch

    def spy(p, t, **k):
        calls.append(t)
        return real(p, t, **k)

    monkeypatch.setattr(regex, "fullmatch", spy)
    f = TagFilter(Rules(exclude_tags=("(1|11)+$",)))
    assert f.allowed("1" * 40 + "!") and f.allowed("1" * 41 + "!")
    assert len(calls) == 1


def test_budget_applies_only_after_a_timeout(monkeypatch):
    import app.rules as r
    monkeypatch.setattr(r, "MATCH_BUDGET", 0.0)
    cheap = r.TagFilter(Rules(include_tags=("1.*",)))
    assert all(cheap.allowed(f"1.{i}") for i in range(50)) and cheap.warnings == []
    f = r.TagFilter(Rules(exclude_tags=("(1|11)+$",)))
    assert f.allowed("1" * 40 + "!")  # times out; the budget is now spent
    assert f.allowed("1.2") is False
    assert any("took too long" in w for w in f.warnings)


async def test_cheap_patterns_never_truncate_big_tag_lists(tmp_path, settings):
    tags = [f"1{i % 10}.{i // 10}.{i % 7}" for i in range(2000)]
    reg = FakeRegistry({"pg:10.0.0": "sha256:old"}, {"pg:10.0.0": tags})
    cs = [info(tmp_path, name=f"db{i}", image="pg:10.0.0", digests=["pg@sha256:old"])
          for i in range(20)]
    rules = Rules(include_tags=("1[0-9]\\.\\d+\\.\\d+",),
                  exclude_tags=(".*-rc.*", ".*alpha.*", ".*-slim"))
    result = await check_all(cs, reg, settings, None, rules_for=lambda _: rules)
    assert all(c.rule_warnings == [] for c in result.containers)
    assert all(c.minor_update is not None for c in result.containers)


@pytest.mark.parametrize("bad", [5, None, [1], "abc", ["(a|b"], {"x": 1}])
def test_hand_edited_list_values_ignored(store, bad):
    store.save_settings({"container_rules": {"site/web": {"include_tags": bad}}})
    rules = RulesStore(store).effective(container())
    assert rules.include_tags == ()
    assert rules.warnings


@pytest.mark.parametrize("field,bad", [
    ("watch", "no"), ("watch", None), ("notify", 0),
    ("release_notes", "javascript:alert(1)"), ("release_notes", 5)])
def test_hand_edited_scalar_values_ignored(store, field, bad):
    store.save_settings({"container_rules": {"site/web": {field: bad}}})
    rules = RulesStore(store).effective(container())
    assert rules == Rules(warnings=rules.warnings) and rules.warnings


def test_hand_edited_row_shapes(store):
    for raw in ({"site/web": "x"}, {"site/web": None}, [], "x", 5):
        store.save_settings({"container_rules": raw})
        assert RulesStore(store).effective(container()) == Rules()
        assert RulesStore(store).snapshot()(container()) == Rules()


def test_snapshot_reads_the_row_once(store, monkeypatch):
    rs = RulesStore(store)
    rs.save("site/web", {"watch": "no"}, "u")
    reads = []
    real = store.load_settings
    monkeypatch.setattr(store, "load_settings", lambda: reads.append(1) or real())
    fn = rs.snapshot()
    for _ in range(5):
        assert not fn(container()).watch
    assert len(reads) == 1
