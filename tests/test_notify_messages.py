import pathlib
from datetime import datetime, timezone

import pytest

from app.checker import CheckResult, ContainerStatus, Status
from app.digest import render_digest
from app.inventory import ContainerInfo
from app.notify.messages import (
    Rendered, UpdateItem, check_failing_message, check_recovered_message, digest_message,
    job_message, layout_message, test_message as make_test_message, updates_found_message, user_pending_message,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def no_url(settings):
    return settings.model_copy(update={"public_url": None})


def _result(status=Status.NEWER_BUILD):
    c = ContainerInfo(
        id="web", name="web", image_ref="nginx:1.25", image_id="sha256:img",
        running_repo_digests=["nginx@sha256:aaaaaaaaaaaaaaaaaaaa"],
        local_tag_image_id="sha256:img", project="site", working_dir="/x",
        config_files=[], service="web", state="running", is_self=False,
    )
    cs = ContainerStatus(container=c, status=status, remote_digest="sha256:bbbbbbbbbbbbbbbbbbbb",
                         minor_update="1.26" if status == Status.NEWER_BUILD else None)
    return CheckResult(datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc), [cs], None)


def test_digest_output_unchanged_from_0_3(settings):
    subject, html, plain = render_digest(_result(), settings)
    assert html == (FIXTURES / "digest_0.3.html").read_text()
    assert plain == (FIXTURES / "digest_0.3.txt").read_text()


def test_digest_message_order_and_none(settings):
    m = digest_message(_result(), settings)
    subject, html, plain = render_digest(_result(), settings)
    assert m == Rendered(subject, plain, html)
    assert m.plain == plain and m.html == html
    quiet = settings.model_copy(update={"digest_when_none": False})
    assert digest_message(_result(Status.UP_TO_DATE), quiet) is None


def test_updates_found(settings):
    items = [UpdateItem("web:image", "web", "nginx:1.25", "1.25 → 1.26"),
             UpdateItem("db:image", "db", "postgres:16", "abc → def")]
    m = updates_found_message(items, settings)
    assert m.subject == "test-server: 2 new updates available"
    for text in (m.plain, m.html):
        assert "web" in text and "nginx:1.25" in text and "1.25 → 1.26" in text
    assert "https://dash.example.com" in m.html and "https://dash.example.com" in m.plain
    assert updates_found_message(items[:1], settings).subject == "test-server: 1 new update available"


def test_updates_found_escapes_html(settings):
    m = updates_found_message([UpdateItem("k", "<b>x</b>", "img", "a → b")], settings)
    assert "<b>x</b>" not in m.html and "&lt;b&gt;x&lt;/b&gt;" in m.html
    assert "<b>x</b>" in m.plain


def test_job_failed_and_finished(settings):
    m = job_message("Update", "web", "failed", "Ann", 7, settings)
    assert m.subject == "test-server: update failed – web"
    assert "Ann" in m.plain and "failed" in m.plain
    assert "https://dash.example.com/jobs/7" in m.plain and "https://dash.example.com/jobs/7" in m.html
    ok = job_message("Update", "web", "succeeded", "Ann", 7, settings)
    assert ok.subject == "test-server: update finished – web"
    assert "succeeded" in ok.html


def test_job_escapes_and_has_no_log_text(settings):
    m = job_message("Update", "<b>x</b>", "failed", "<i>u</i>", 1, settings)
    assert "<b>x</b>" not in m.html and "<i>u</i>" not in m.html
    assert "log" not in m.plain.lower().replace("login", "")


def test_check_failing_and_recovered(settings):
    f = check_failing_message(3, settings)
    assert f.subject == "test-server: update check failing"
    assert "3" in f.plain and "3" in f.html
    r = check_recovered_message(settings)
    assert r.subject == "test-server: update check recovered"
    assert "working again" in r.plain and "working again" in r.html


def test_layout(settings):
    m = layout_message(["web: <b>bad</b>", "db: missing"], settings)
    assert m.subject == "test-server: layout problems"
    assert "db: missing" in m.plain and "db: missing" in m.html
    assert "<b>bad</b>" not in m.html and "&lt;b&gt;" in m.html


def test_user_pending(settings):
    m = user_pending_message("Ann <x>", "ann@example.com", settings)
    assert m.subject == "test-server: user waiting for approval"
    assert "ann@example.com" in m.plain and "ann@example.com" in m.html
    assert "https://dash.example.com/users" in m.plain and "https://dash.example.com/users" in m.html
    assert "Ann <x>" not in m.html


def test_test_message(settings):
    m = make_test_message("Ops <email>", settings)
    assert m.subject == "test-server: test notification"
    assert "This is a test from Docker Manager on test-server." in m.plain
    assert "If you can read this, the Ops <email> channel works." in m.plain
    assert "Ops &lt;email&gt;" in m.html


def test_no_links_without_public_url(no_url):
    msgs = [
        updates_found_message([UpdateItem("k", "web", "i", "c")], no_url),
        job_message("Update", "web", "failed", "Ann", 7, no_url),
        check_failing_message(2, no_url), check_recovered_message(no_url),
        layout_message(["p"], no_url), user_pending_message("A", "a@b.c", no_url),
        make_test_message("c", no_url),
    ]
    for m in msgs:
        assert "http" not in m.plain and "href" not in m.html and "/jobs/" not in m.plain


def test_html_looks_like_digest(settings):
    m = job_message("Update", "web", "failed", "Ann", 7, settings)
    assert 'lang="en-GB"' in m.html and "max-width:640px" in m.html
    assert "test-server:" in m.html


def test_subject_is_collapsed_to_one_line(settings):
    s = settings.model_copy(update={"server_name": "my\r\n  server\tname"})
    assert "\n" not in check_recovered_message(s).subject
    assert check_recovered_message(s).subject == "my server name: update check recovered"
    assert digest_message(_result(), s).subject.startswith("my server name: ")


def test_updates_found_release_notes_link(settings):
    from app.notify.messages import updates_found_message
    with_notes = UpdateItem("a:minor", "a", "img", "1 → 2", 'https://x.test/?a=1&b=2')
    m = updates_found_message([with_notes, UpdateItem("b:minor", "b", "img", "1 → 2")], settings)
    assert 'href="https://x.test/?a=1&amp;b=2"' in m.html and 'rel="noopener noreferrer"' in m.html
    assert m.html.count("Release notes") == 1 and m.plain.count("release notes:") == 1
