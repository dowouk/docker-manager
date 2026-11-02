from datetime import datetime, timezone

import pytest

from app import view
from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status
from app.inventory import ContainerInfo

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)


def cs(name, st=Status.UP_TO_DATE, image="nginx:latest", project="site", **kw):
    c = ContainerInfo(
        id=name.ljust(64, "0"), name=name, image_ref=image, image_id="sha256:img",
        running_repo_digests=[], local_tag_image_id="sha256:img", project=project,
        working_dir="/docker/x", config_files=[], service=name, state="running", is_self=False,
    )
    return ContainerStatus(container=c, status=st, **kw)


def test_row_state_matches_what_the_page_shows():
    assert view.row_state(cs("a")) == "current"
    assert view.row_state(cs("a", Status.NEWER_BUILD)) == "update"
    assert view.row_state(cs("a", Status.PULLED_NOT_RECREATED)) == "update"
    assert view.row_state(cs("a", minor_update="1.2")) == "update"
    assert view.row_state(cs("a", major_update="2.0")) == "update"
    assert view.row_state(cs("a", Status.NOT_CHECKABLE)) == "unchecked"
    # a failed check wins over a stale version hint
    assert view.row_state(cs("a", Status.ERROR, minor_update="1.2")) == "error"


def test_by_urgency_updates_then_errors_then_current_by_name():
    result = CheckResult(NOW, [cs("zed"), cs("err", Status.ERROR), cs("b", Status.NEWER_BUILD),
                               cs("a", minor_update="1.1"), cs("abc")])
    assert [r.container.name for r in view.by_urgency(result)] == ["a", "b", "err", "abc", "zed"]


def test_overview_counts_and_percentage_include_mailcow():
    result = CheckResult(
        NOW,
        [cs("a"), cs("b"), cs("c", Status.NEWER_BUILD), cs("d", Status.ERROR, project=None),
         cs("e", major_update="3.0")],
        mailcow=MailcowStatus(update_available=False, current_version="2026-10"),
    )
    o = view.overview(result)
    assert (o.total, o.current, o.updates, o.errors, o.majors) == (6, 3, 2, 1, 1)
    assert o.attention == 3
    assert o.percent_current == 50
    assert o.stacks == 3  # "site", the stack-less "d" on its own, and Mailcow


def test_unchecked_images_are_neutral():
    result = CheckResult(NOW, [cs("a"), cs("b", Status.NEWER_BUILD), cs("local", Status.NOT_CHECKABLE)])
    o = view.overview(result)
    assert (o.total, o.current, o.updates, o.errors, o.unchecked) == (3, 1, 1, 0, 1)
    assert o.percent_current == 50  # of the two that could be checked
    assert o.attention == 1
    assert [c.name for c in view.attention(result)] == ["b"]
    assert [r.container.name for r in view.by_urgency(result)] == ["b", "a", "local"]


def test_overview_with_nothing_running():
    o = view.overview(CheckResult(NOW, []))
    assert (o.total, o.percent_current, o.attention) == (0, 0, 0)


@pytest.mark.parametrize("st, message, badge", [
    (Status.ERROR, "docker.n8n.io rate limit reached – try again later", "Rate limited"),
    (Status.ERROR, "ghcr.io token request returned HTTP 401", "Auth failed · 401"),
    (Status.ERROR, "registry returned HTTP 4010 bytes", "Error"),
    (Status.ERROR, None, "Error"),
])
def test_error_cards_name_the_problem(st, message, badge):
    card, = view.attention(CheckResult(NOW, [cs("x", st, image="ghcr.io/o/x:1", message=message)]))
    assert (card.state, card.badge, card.tag) == ("error", badge, "ghcr.io")
    assert card.text == (message or "The check failed.")


def test_attention_lists_updates_before_errors_and_skips_current():
    result = CheckResult(
        NOW,
        [cs("ok"), cs("broken", Status.ERROR, message="boom"), cs("web", Status.NEWER_BUILD),
         cs("db", minor_update="16.3"), cs("pg", major_update="17.0")],
        mailcow=MailcowStatus(update_available=True, current_version="2026-09", latest_release="2026-10"),
    )
    cards = view.attention(result)
    assert [(c.name, c.badge) for c in cards] == [
        ("Mailcow", "Update available"), ("db", "New version"), ("pg", "Major version"),
        ("web", "Newer build"), ("broken", "Error"),
    ]
    assert "2026-10" in cards[0].text and cards[0].cs is None
    assert "update by hand" in cards[2].text


def test_mailcow_error_card_comes_last():
    result = CheckResult(NOW, [cs("web", Status.NEWER_BUILD)], mailcow=MailcowStatus(error="folder not mounted"))
    assert [c.name for c in view.attention(result)] == ["web", "Mailcow"]


@pytest.mark.parametrize("name, expected", [
    ("caddy", "ca"), ("uptime-kuma", "uk"), ("readdy-mcp", "rm"), ("n8n", "n8"),
    ("n8n-n8n-1", "nn"), ("web_1", "we"), ("", "?"),
])
def test_initials(name, expected):
    assert view.initials(name) == expected


def test_user_name_helpers():
    assert view.user_initials("Steven Hardy") == "SH"
    assert view.user_initials("Cher") == "C"
    assert view.user_initials("Steve Hardy (DOWO Digital)") == "SH"
    assert view.user_initials("Ann Lee [Contractor]") == "AL"
    assert view.user_initials("(Shared)") == "S"
    assert view.user_initials("") == "?"
    assert view.first_name("Steven Hardy") == "Steven"
    assert view.first_name("") == "there"


@pytest.mark.parametrize("hour, text", [(0, "Good morning"), (11, "Good morning"), (12, "Good afternoon"),
                                        (17, "Good afternoon"), (18, "Good evening"), (23, "Good evening")])
def test_greeting(hour, text):
    assert view.greeting(NOW.replace(hour=hour)) == text


@pytest.mark.parametrize("change, kw, expected", [
    ({}, {}, ["restart", "stop"]),
    ({"state": "exited"}, {}, ["start"]),
    ({"state": "paused"}, {}, []),
    ({"is_self": True}, {}, []),
    ({"service": None}, {}, []),
    ({}, {"fronts_dashboard": True}, ["restart"]),  # stopping the proxy cuts off the page
    ({}, {"updatable": False, "not_updatable_reason": "Not started by compose"}, []),
])
def test_control_actions(change, kw, expected):
    row = cs("web", **kw)
    for key, value in change.items():
        setattr(row.container, key, value)
    assert view.control_actions(row) == expected


def test_config_editable():
    row = cs("web")
    assert not view.config_editable(row)  # compose recorded no files
    row.container.config_files = ["/docker/x/compose.yml"]
    assert view.config_editable(row)
    row.container.project = None
    assert not view.config_editable(row)


def _state(status, hours=0, minutes=0):
    from datetime import timedelta
    from app.prefetch import State
    return State(status, NOW - timedelta(hours=hours, minutes=minutes))


def test_prefetch_note_version_ready():
    seen = []

    def state_for(key):
        seen.append(key)
        return _state("ready", hours=2)

    note = view.prefetch_note(cs("db", image="postgres:16.2", minor_update="16.4"), state_for, NOW)
    assert seen == ["postgres:16.4"]
    assert note == view.PrefetchNote("16.4 ready", "Downloaded 2 h ago", True)


def test_prefetch_note_version_downloading():
    note = view.prefetch_note(cs("db", image="postgres:16.2", minor_update="16.4"),
                              lambda k: _state("downloading"), NOW)
    assert note == view.PrefetchNote("16.4 – downloading…", None, False)


def test_prefetch_note_build_failed():
    seen = []

    def state_for(key):
        seen.append(key)
        return _state("failed")

    note = view.prefetch_note(cs("web", Status.NEWER_BUILD), state_for, NOW)
    assert seen == ["nginx:latest"]
    assert note == view.PrefetchNote(
        None, "Couldn't download ahead – will try again at the next check", False)


def test_prefetch_note_build_ready_shows_badge_and_time():
    note = view.prefetch_note(cs("web", Status.NEWER_BUILD), lambda k: _state("ready", minutes=5), NOW)
    assert note == view.PrefetchNote("Newer build ready", "Downloaded 5 min ago", True)


def test_prefetch_note_pulled_not_recreated_is_ready_without_state():
    note = view.prefetch_note(cs("web", Status.PULLED_NOT_RECREATED), lambda k: None, NOW)
    assert note == view.PrefetchNote(None, None, True)


def test_prefetch_note_pulled_not_recreated_with_state_has_no_second_badge():
    note = view.prefetch_note(cs("web", Status.PULLED_NOT_RECREATED), lambda k: _state("ready", hours=3), NOW)
    assert note == view.PrefetchNote(None, "Downloaded 3 h ago", True)


def test_prefetch_note_none_without_update_or_state():
    assert view.prefetch_note(cs("web"), lambda k: _state("ready"), NOW) is None
    assert view.prefetch_note(cs("web", Status.NEWER_BUILD), lambda k: None, NOW) is None
    assert view.prefetch_note(cs("db", minor_update="16.4"), lambda k: None, NOW) is None
    assert view.prefetch_note(cs("db", major_update="17.0"), lambda k: _state("ready"), NOW) is None


def test_ago_wording():
    from datetime import timedelta
    assert view._ago(NOW - timedelta(seconds=20), NOW) is None
    assert view._ago(NOW - timedelta(minutes=7), NOW) == "7 min"
    assert view._ago(NOW - timedelta(hours=5), NOW) == "5 h"
    assert view._ago(NOW - timedelta(days=3), NOW) == "3 days"
    assert view._ago(NOW - timedelta(days=1), NOW) == "1 day"


def test_update_card_for_pulled_build_says_ready():
    card = view._update_card(cs("web", Status.PULLED_NOT_RECREATED))
    assert card.badge == "Ready"
    assert card.text == "The newer build is downloaded – updating restarts it on the new image."


def test_prefetch_note_just_downloaded_has_no_ago():
    note = view.prefetch_note(cs("web", Status.NEWER_BUILD), lambda k: _state("ready"), NOW)
    assert note.text == "Downloaded just now"


def _unused_result(size, count=9):
    from app.images import UnusedSummary
    return CheckResult(NOW, [], None, unused_images=UnusedSummary(count, size))


def test_card_shows_over_threshold():
    cards = view.attention(_unused_result(6_400_000_000), unused_warn_gb=5)
    assert [(c.state, c.name, c.text, c.cs) for c in cards] == [
        ("images", "Unused images", "9 images, 6.4 GB could be freed.", None)]


def test_no_card_under_threshold_or_when_off():
    r = _unused_result(6_400_000_000)
    assert view.attention(r, unused_warn_gb=10) == []
    assert view.attention(r, unused_warn_gb=0) == []
    assert view.attention(r) == []


def test_no_card_when_scan_failed():
    assert view.attention(CheckResult(NOW, [], None), unused_warn_gb=5) == []
