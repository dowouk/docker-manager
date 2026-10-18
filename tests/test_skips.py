import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.checker import CheckResult, ContainerStatus, MailcowStatus, Status
from app.notify.notifier import current_updates
from app.roles import Role
from app.skips import ItemRef, SkipStore, update_items, visible_result
from app.store import Store
from app.view import attention, overview
from auth_helpers import CSRF, logged_in_client
from test_routes import CHECKED_AT, compose_file, info, make_app, seeded, status  # noqa: F401

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def skips(tmp_path, clock):
    return SkipStore(Store(str(tmp_path / "s.db")), clock)


def result_with(tmp_path, digest="sha256:aaa", minor="16.3", major="17.0", mailcow="2026-10"):
    web = info(tmp_path, "web")
    db = info(tmp_path, "db", image="postgres:16.2")
    return CheckResult(
        checked_at=CHECKED_AT,
        containers=[status(web, Status.NEWER_BUILD, remote_digest=digest),
                    status(db, minor_update=minor, major_update=major)],
        mailcow=MailcowStatus(update_available=True, current_version="2026-09",
                              latest_release=mailcow),
    )


def test_item_keys_match_the_notifier(tmp_path):
    r = result_with(tmp_path)
    assert {i.key for i in update_items(r)} == set(current_updates(r))
    by_key = {i.key: i for i in update_items(r)}
    assert by_key["web:build"] == ItemRef("web:build", "site/web", "build", "sha256:aaa",
                                          r.containers[0].container.id)
    assert by_key["db:minor"].value == "16.3" and by_key["db:major"].value == "17.0"
    assert by_key["mailcow"].container_id is None and by_key["mailcow"].value == "2026-10"


def test_skip_hides_only_that_value(tmp_path, skips):
    skips.skip("site/web", "build", "sha256:aaa", "me", "")
    visible, hidden = visible_result(result_with(tmp_path), skips)
    assert visible.containers[0].status == Status.UP_TO_DATE
    assert [(cs.container.name, s.value) for cs, s in hidden] == [("web", "sha256:aaa")]
    newer, hidden = visible_result(result_with(tmp_path, digest="sha256:bbb"), skips)
    assert newer.containers[0].status == Status.NEWER_BUILD and hidden == []


def test_skip_tag_clears_only_that_kind(tmp_path, skips):
    skips.skip("site/db", "minor", "16.3", "me", "")
    visible, _ = visible_result(result_with(tmp_path), skips)
    db = visible.containers[1]
    assert db.minor_update is None and db.major_update == "17.0"
    newer, _ = visible_result(result_with(tmp_path, minor="16.4"), skips)
    assert newer.containers[1].minor_update == "16.4"


def test_build_update_without_digest_is_skippable(tmp_path, skips):
    r = result_with(tmp_path, digest=None)
    item = next(i for i in update_items(r) if i.kind == "build")
    assert item.value == "latest"
    skips.skip(item.container_key, "build", item.value, "me")
    assert visible_result(r, skips)[0].containers[0].status == Status.UP_TO_DATE


def test_active_reads_without_writing_unless_something_expired(skips, clock, monkeypatch):
    skips.snooze("site/web", "build", 7, "me")
    monkeypatch.setattr(skips._store, "write_tx", lambda: pytest.fail("wrote"))
    assert len(skips.active()) == 1


def test_original_result_is_not_modified(tmp_path, skips):
    skips.skip("site/web", "build", "sha256:aaa", "me", "")
    r = result_with(tmp_path)
    skips.skip("site/db", "minor", "16.3", "me", "")
    skips.skip("site/db", "major", "17.0", "me", "")
    visible_result(r, skips)
    assert r.containers[0].status == Status.NEWER_BUILD
    assert (r.containers[1].minor_update, r.containers[1].major_update) == ("16.3", "17.0")
    assert r.mailcow.update_available


def test_snooze_hides_any_value_until_expiry(tmp_path, skips, clock):
    skips.snooze("site/web", "build", 7, "me", "later")
    r = result_with(tmp_path, digest="sha256:zzz")
    assert visible_result(r, skips)[0].containers[0].status == Status.UP_TO_DATE
    clock.now = T0 + timedelta(days=7) - timedelta(seconds=1)
    assert visible_result(r, skips)[0].containers[0].status == Status.UP_TO_DATE
    clock.now = T0 + timedelta(days=7)
    assert visible_result(r, skips)[0].containers[0].status == Status.NEWER_BUILD
    assert skips.active() == []  # purged


def test_snooze_30_days_and_bad_days(skips, clock):
    s = skips.snooze("site/web", "minor", 30, "me")
    assert s.until == T0 + timedelta(days=30)
    with pytest.raises(ValueError):
        skips.snooze("site/web", "minor", 5, "me")  # type: ignore[arg-type]


def test_remove_restores_immediately(tmp_path, skips):
    s = skips.skip("site/web", "build", "sha256:aaa", "me")
    assert skips.remove(s.id).id == s.id
    assert visible_result(result_with(tmp_path), skips)[0].containers[0].status == Status.NEWER_BUILD
    assert skips.remove(s.id) is None


def test_new_skip_replaces_the_old_for_same_container_and_kind(skips):
    skips.skip("site/web", "build", "sha256:aaa", "me")
    skips.snooze("site/web", "build", 7, "me")
    assert len(skips.active()) == 1


def test_mailcow_skip(tmp_path, skips):
    skips.skip("mailcow", "mailcow", "2026-10", "me")
    visible, hidden = visible_result(result_with(tmp_path), skips)
    assert visible.mailcow.update_available is False
    assert hidden[0][0] is None and hidden[0][1].kind == "mailcow"
    assert visible_result(result_with(tmp_path, mailcow="2026-11"), skips)[0].mailcow.update_available


def test_counts_and_attention_exclude_skipped(tmp_path, skips):
    r = result_with(tmp_path)
    assert overview(r).updates == 3  # web build, db, mailcow
    skips.skip("site/web", "build", "sha256:aaa", "me")
    skips.skip("mailcow", "mailcow", "2026-10", "me")
    skips.skip("site/db", "major", "17.0", "me")
    visible, _ = visible_result(r, skips)
    assert overview(visible).updates == 1 and overview(visible).majors == 0
    assert [a.name for a in attention(visible)] == ["db"]
    assert visible.summary()["updates"] == 1


def test_reason_is_capped_and_one_line(skips):
    s = skips.skip("site/web", "build", "sha256:aaa", "me", "x" * 500 + "\n")
    assert len(s.reason) == 200
    assert skips.skip("site/web", "minor", "1", "me", "a\n b").reason == "a b"


def test_migration_4_on_a_0_4_database(tmp_path):
    path = str(tmp_path / "d.db")
    s = Store(path)
    s.set_meta("existing_install", "1")
    s.set_meta("upgraded_from_0_1", "1")
    s.save_settings({"server_name": "box"})
    s.create_job("build", "web", "me")
    user = sqlite3.connect(path)  # a user row, written directly
    user.execute("INSERT INTO users (kind, email, name, role, created_at) "
                 "VALUES ('local', 'a@b.c', 'A', 'admin', '2026-01-01T00:00:00+00:00')")
    user.commit(); user.close()
    with s.write_tx() as db:  # now shape it as 0.4 left it: schema 3, no skips table
        db.execute("DROP TABLE skips")
        db.execute("UPDATE schema_version SET version = 3")
    s.close()
    s = Store(path)
    assert sqlite3.connect(path).execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert s.get_meta("existing_install") == "1" and s.get_meta("upgraded_from_0_1") == "1"
    assert s.load_settings() == {"server_name": "box"}
    assert [j.target for j in s.list_jobs()] == ["web"]
    assert sqlite3.connect(path).execute("SELECT email FROM users").fetchall() == [("a@b.c",)]
    SkipStore(s).skip("site/web", "build", "x", "me")
    with pytest.raises(sqlite3.IntegrityError):
        with s.write_tx() as db:
            db.execute("INSERT INTO skips (key, kind, user, created_at) VALUES ('a','bogus','u','t')")


# -- routes --------------------------------------------------------------------------------

def post(c, path, **data):
    return c.post(path, data={"csrf_token": CSRF, **data})


def cid(app, name):
    return next(cs.container.id for cs in app.state.store and
                CheckResult.from_dict(app.state.store.latest_check()[1]).containers
                if cs.container.name == name)


def audits(app):
    return [a for a in app.state.store.list_audit() if a.kind == "updates"]


def test_skip_route_hides_update_and_audits(settings, tmp_path, seeded):  # noqa: F811
    seeded.containers[0].remote_digest = "sha256:aaa"
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        r = post(c, f"/containers/{cid(app, 'web')}/skip", kind="build", mode="skip",
                 reason="<b>wait</b>")
        assert r.status_code == 303
        page = c.get("/").text
    assert [s.value for s in app.state.skips.active()] == ["sha256:aaa"]
    assert "Skipped" in page and "&lt;b&gt;wait&lt;/b&gt;" in page and "<b>wait</b>" not in page
    entry = audits(app)[0]
    assert "site/web" in entry.summary and "<b>wait</b>" in entry.summary


def test_snooze_route(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        assert post(c, f"/containers/{cid(app, 'db')}/skip", kind="minor", mode="snooze30").status_code == 303
    s = app.state.skips.active()[0]
    assert s.kind == "minor" and s.value is None and s.until is not None
    assert "30 days" in audits(app)[0].summary


def test_mailcow_skip_route(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        assert post(c, "/containers/mailcow/skip", kind="mailcow", mode="skip").status_code == 303
    assert app.state.skips.active()[0].value == "2026-10"


def test_only_offered_updates_can_be_skipped(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    db = cid(app, "db")
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        assert post(c, f"/containers/{db}/skip", kind="build", mode="skip").status_code == 400
        assert post(c, f"/containers/{db}/skip", kind="minor", mode="skip", value="99.9").status_code == 400
        assert post(c, f"/containers/{db}/skip", kind="bogus", mode="skip").status_code == 400
        assert post(c, f"/containers/{db}/skip", kind="minor", mode="forever").status_code == 400
        assert post(c, "/containers/nope/skip", kind="minor", mode="skip").status_code == 404
        assert post(c, f"/containers/{cid(app, 'lone')}/skip", kind="minor", mode="skip").status_code == 400
    assert app.state.skips.active() == [] and audits(app) == []


def test_viewer_forbidden_and_csrf(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    web = cid(app, "web")
    with logged_in_client(app, Role.VIEWER) as c:
        assert post(c, f"/containers/{web}/skip", kind="build", mode="skip").status_code == 403
        assert "Skip…" not in c.get("/").text
    with logged_in_client(app, Role.OPERATOR) as c:
        r = c.post(f"/containers/{web}/skip", data={"kind": "build", "mode": "skip", "csrf_token": "bad"})
        assert r.status_code == 403
        assert "Skip…" in c.get("/").text
    assert app.state.skips.active() == []


def test_remove_route_restores_and_audits(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    s = app.state.skips.snooze("site/db", "minor", 7, "x@y.z", "busy")
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        assert post(c, f"/skips/{s.id}/remove").status_code == 303
        assert post(c, f"/skips/{s.id}/remove").status_code == 404
    assert app.state.skips.active() == []
    assert "Removed" in audits(app)[0].summary


def test_reason_capped_by_route(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        post(c, f"/containers/{cid(app, 'db')}/skip", kind="minor", mode="skip", reason="y" * 900)
    assert len(app.state.skips.active()[0].reason) == 200
    assert len(audits(app)[0].summary) < 400


def test_dashboard_counts_exclude_skipped(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    db = cid(app, "db")
    with logged_in_client(app, Role.ADMIN, follow_redirects=False) as c:
        before = c.get("/").text
        assert "Update Mailcow" in before and f"/containers/{db}/version-preview?tag=16.3" in before
        assert "5 need" in before
        post(c, f"/containers/{db}/skip", kind="minor", mode="skip")
        post(c, f"/containers/{db}/skip", kind="major", mode="skip")
        post(c, "/containers/mailcow/skip", kind="mailcow", mode="snooze7")
        after = c.get("/").text
        assert "Update Mailcow" not in after
        assert "version-preview?tag=16.3" not in after
        assert "3 need" in after  # was 5: db and Mailcow are hidden
        assert "Skipped" in after and "Snoozed until" in after
        for s in app.state.skips.active():
            assert post(c, f"/skips/{s.id}/remove").status_code == 303
        back = c.get("/").text
    assert "Update Mailcow" in back and "version-preview?tag=16.3" in back and "5 need" in back
