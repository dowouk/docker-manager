from datetime import datetime, timezone

import pytest

from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status
from app.digest import render_digest
from app.inventory import ContainerInfo

CHECKED_AT = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)  # 08:00 BST


def cs(name, st=Status.UP_TO_DATE, image="nginx:1.25", **kw):
    c = ContainerInfo(
        id=name, name=name, image_ref=image, image_id="sha256:img",
        running_repo_digests=["nginx@sha256:aaaaaaaaaaaaaaaaaaaa"],
        local_tag_image_id="sha256:img", project="site", working_dir="/x",
        config_files=[], service=name, state="running", is_self=False,
    )
    return ContainerStatus(container=c, status=st, **kw)


def result(containers=(), mailcow=None):
    return CheckResult(CHECKED_AT, list(containers), mailcow)


def test_subject_all_up_to_date(settings):
    subject, html, plain = render_digest(result([cs("web")]), settings)
    assert subject == "test-server: all up to date"
    assert "Newer build available" not in html and "Mailcow" not in plain


def test_subject_counts_distinct_containers_and_mailcow(settings):
    r = result(
        [
            cs("a", Status.NEWER_BUILD, minor_update="1.26", major_update="2.0"),
            cs("b", Status.PULLED_NOT_RECREATED),
            cs("c", minor_update="1.25.1"),
            cs("d"),
        ],
        MailcowStatus(update_available=True, current_version="2026-09", latest_release="2026-10"),
    )
    subject, html, plain = render_digest(r, settings)
    assert subject == "test-server: 4 updates available"
    for text in (html, plain):
        positions = [
            text.index(h)
            for h in ("Mailcow", "Newer build available", "Minor/patch updates",
                      "Major updates (update by hand)")
        ]
        assert positions == sorted(positions)
        assert "2026-09 → 2026-10" in text
        assert "1.25 → 1.26" in text and "1.25 → 2.0" in text
    assert "Couldn't check" not in html
    assert "17 Oct 2026, 08:00" in html and "https://dash.example.com" in html


def test_singular_subject(settings):
    subject, _, _ = render_digest(result([cs("a", Status.NEWER_BUILD)]), settings)
    assert subject == "test-server: 1 update available"


def test_errors_listed_and_escaped(settings):
    r = result(
        [cs("odd", Status.ERROR, message="<b>boom</b>"), cs("pin", Status.NOT_CHECKABLE,
                                                          message="Pinned by digest")],
        MailcowStatus(error="update.sh failed"),
    )
    subject, html, plain = render_digest(r, settings)
    assert subject == "test-server: all up to date"
    assert "Couldn&#39;t check" in html and "Couldn't check" in plain
    assert "&lt;b&gt;boom&lt;/b&gt;" in html and "<b>boom</b>" not in html
    assert "<b>boom</b>" in plain
    assert "update.sh failed" in plain
    # a pinned or local image isn't something that went wrong
    assert "Pinned by digest" not in html and "pin" not in plain.split("Couldn't check", 1)[1]


def test_none_when_nothing_and_disabled(settings):
    s = settings.model_copy(update={"digest_when_none": False})
    assert render_digest(result([cs("web")]), s) is None
    assert render_digest(result([cs("web", Status.NEWER_BUILD)]), s) is not None


def test_tag_listing_failure_listed_under_couldnt_check(settings):
    r = result([cs("web", Status.UP_TO_DATE, message="Could not list tags: Docker Hub said no")])
    _, html, plain = render_digest(r, settings)
    for text in (html, plain):
        assert "Could not list tags: Docker Hub said no" in text
    assert "Couldn't check" in plain


def test_major_only_not_counted_in_subject(settings):
    r = result([cs("db", major_update="2.0"), cs("web", Status.NEWER_BUILD)])
    subject, html, _ = render_digest(r, settings)
    assert subject == "test-server: 1 update available"
    assert "1.25 → 2.0" in html
    subject, _, _ = render_digest(result([cs("db", major_update="2.0")]), settings)
    assert subject == "test-server: all up to date"


@pytest.mark.parametrize("problem", [
    cs("odd", Status.ERROR, message="boom"),
    cs("tags", message="Could not list tags: nope"),
])
def test_when_none_disabled_still_sends_problems(settings, problem):
    s = settings.model_copy(update={"digest_when_none": False})
    assert render_digest(result([cs("web"), problem]), s) is not None
    assert render_digest(result([cs("web")], MailcowStatus(error="x")), s) is not None


def test_no_public_url_means_no_link(settings):
    s = settings.model_copy(update={"public_url": None})
    _, html, text = render_digest(result([cs("web")]), s)
    assert "Open the dashboard" not in html and "Dashboard:" not in text
    assert "None" not in html and "None" not in text


# ---- skips, notify rule, layout, release notes (0.5) ----

from app.layout import LayoutProblem


def prob(key, container="web", detail="Bind mount outside /srv"):
    return LayoutProblem(key, container, "site", "bind_outside", detail, "hint", "problem")


def test_skipped_note_only_when_something_was_skipped(settings):
    r = result([cs("a", minor_update="1.1")])
    _, html, plain = render_digest(r, settings, skipped=2)
    assert "2 updates skipped or snoozed" in html and "2 updates skipped or snoozed" in plain
    assert "1 update skipped or snoozed" in render_digest(r, settings, skipped=1)[2]
    assert "skipped" not in render_digest(r, settings)[2]


def test_notify_false_container_absent_from_digest(settings):
    r = result([cs("quiet", minor_update="1.1", notify=False), cs("loud", minor_update="1.2")])
    subject, html, _ = render_digest(r, settings)
    assert "quiet" not in html and "loud" in html and "1 update available" in subject


def test_layout_section_counts_caps_and_links(settings):
    r = result([cs("web")])
    r.layout = [prob(f"k{i}", detail=f"d{i}") for i in range(12)] + [prob("k0")]  # dup key
    _, html, plain = render_digest(r, settings)
    assert "12 layout problems" in html and "12 layout problems" in plain
    assert "web: d9" in plain and "web: d10" not in plain and "and 2 more" in plain
    assert 'href="https://dash.example.com/layout"' in html
    assert "Layout page: https://dash.example.com/layout" in plain


def test_layout_section_leaves_out_quiet_containers_and_absent_when_none(settings):
    r = result([cs("web", notify=False), cs("db")])
    r.layout = [prob("a", "web"), prob("b", "db", "mine")]
    _, html, _ = render_digest(r, settings)
    assert "1 layout problem" in html and "db: mine" in html and "web:" not in html
    r.layout = []
    assert "/layout" not in render_digest(r, settings)[1]
    r.layout = None
    assert "/layout" not in render_digest(r, settings)[1]


def test_release_notes_link_is_escaped_and_safe(settings):
    r = result([cs("a", minor_update="1.1")])
    _, html, plain = render_digest(r, settings, notes_for=lambda info: 'https://x.test/?a=1&b="2"')
    assert 'rel="noopener noreferrer"' in html and "a=1&amp;b=&#34;2&#34;" in html
    assert "release notes: https://x.test/" in plain
    assert "Release notes" not in render_digest(r, settings)[1]


def test_layout_problems_alone_send_the_digest(settings):
    s = settings.model_copy(update={"digest_when_none": False}) if hasattr(settings, "model_copy") \
        else __import__("dataclasses").replace(settings, digest_when_none=False)
    r = result([cs("web")])
    assert render_digest(r, s) is None
    r.layout = [prob("a")]
    out = render_digest(r, s)
    assert out is not None and "1 layout problem" in out[1]
    assert out[0] == "test-server: 1 layout problem" and "all up to date" not in out[0]
    quiet = result([cs("web", notify=False)])
    quiet.layout = [prob("a")]
    assert render_digest(quiet, s) is None


def test_layout_detail_is_escaped_in_digest(settings):
    r = result([cs("web")])
    r.layout = [prob("a", detail="<b>x</b> & y")]
    html = render_digest(r, settings)[1]
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in html and "<b>x</b>" not in html


def test_layout_subject_counts_problems_and_notices_separately(settings):
    r = result([cs("web")])
    r.layout = [prob("a"), prob("b"), prob("c", detail="Not started by compose")]
    r.layout[2] = LayoutProblem("c", "web", None, "not_compose", "Not started by compose", "h", "notice")
    subject, html, plain = render_digest(r, settings)
    assert subject.endswith("2 layout problems and 1 notice")
    assert "2 layout problems and 1 notice" in html and "2 layout problems and 1 notice" in plain
    r.layout = [r.layout[2]]
    subject, html, _ = render_digest(r, settings)
    assert subject.endswith(": 1 notice") and "layout problem" not in html
    r.layout = [prob("a")]
    r.containers[0].minor_update = "1.1"
    assert render_digest(r, settings)[0].endswith("1 update available")
