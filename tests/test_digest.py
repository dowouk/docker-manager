from datetime import datetime, timezone

import pytest

from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status
from app.digest import render_digest, send_digest, send_test_email
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


@pytest.fixture
def digest_settings(settings):
    return settings.model_copy(update={
        "acs_connection_string": "endpoint=https://x;accesskey=k",
        "digest_from": "dash@example.com",
        "digest_to": ["a@example.com", "b@example.com"],
    })


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


class FakeClient:
    def __init__(self):
        self.messages = []

    def begin_send(self, message):
        self.messages.append(message)

        class Poller:
            def result(self_inner):
                return {"status": "Succeeded"}

        return Poller()


def test_send_uses_factory_and_recipients(digest_settings):
    client, seen = FakeClient(), []

    def factory(conn):
        seen.append(conn)
        return client

    out = send_digest(digest_settings, result([cs("a", Status.NEWER_BUILD)]), factory)
    assert out == "sent"
    assert seen == ["endpoint=https://x;accesskey=k"]
    (msg,) = client.messages
    assert msg["senderAddress"] == "dash@example.com"
    assert msg["recipients"] == {"to": [{"address": "a@example.com"}, {"address": "b@example.com"}]}
    assert msg["content"]["subject"] == "test-server: 1 update available"
    assert msg["content"]["html"] and msg["content"]["plainText"]


def test_send_not_configured(settings):
    def factory(conn):
        raise AssertionError("must not be called")

    assert send_digest(settings, result(), factory) == "skipped – email not configured"


def test_send_skipped_nothing_to_report(digest_settings):
    s = digest_settings.model_copy(update={"digest_when_none": False})

    def factory(conn):
        raise AssertionError("must not be called")

    assert send_digest(s, result([cs("a")]), factory) == "skipped – nothing to report"


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


def test_send_test_email(digest_settings):
    client = FakeClient()
    send_test_email(digest_settings, lambda conn: client)
    (msg,) = client.messages
    assert msg["content"]["subject"] == "test-server: test email"
    assert msg["recipients"] == {"to": [{"address": "a@example.com"}, {"address": "b@example.com"}]}
    assert "email is working" in msg["content"]["plainText"]
    assert "test-server" in msg["content"]["html"]


def test_send_test_email_unconfigured_raises(settings):
    with pytest.raises(ValueError, match="Email is not configured"):
        send_test_email(settings, lambda conn: FakeClient())


def test_no_public_url_means_no_link(settings):
    s = settings.model_copy(update={"public_url": None})
    _, html, text = render_digest(result([cs("web")]), s)
    assert "Open the dashboard" not in html and "Dashboard:" not in text
    assert "None" not in html and "None" not in text
