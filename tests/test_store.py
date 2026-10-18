import sqlite3
from datetime import datetime, timedelta, timezone

from app.store import Store


def make(tmp_path):
    return Store(str(tmp_path / "sub" / "dash.db"))


def test_check_round_trip(tmp_path):
    s = make(tmp_path)
    assert s.latest_check() is None
    when = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
    s.save_check({"a": [1, 2], "b": "x"}, when)
    got_when, payload = s.latest_check()
    assert got_when == when
    assert got_when.tzinfo is not None
    assert payload == {"a": [1, 2], "b": "x"}


def test_latest_check_returns_newest(tmp_path):
    s = make(tmp_path)
    t = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
    s.save_check({"n": 2}, t + timedelta(hours=6))
    s.save_check({"n": 1}, t)
    when, payload = s.latest_check()
    assert payload == {"n": 2}
    assert when == t + timedelta(hours=6)


def test_job_lifecycle(tmp_path):
    s = make(tmp_path)
    jid = s.create_job("compose", "web", "alice")
    job = s.get_job(jid)
    assert job.status == "running"
    assert (job.kind, job.target, job.user) == ("compose", "web", "alice")
    assert job.started_at.tzinfo is not None
    assert job.finished_at is None
    assert job.log == ""
    s.append_log(jid, "one\n")
    s.append_log(jid, "two\n")
    assert s.get_job(jid).log == "one\ntwo\n"
    s.finish_job(jid, "succeeded")
    job = s.get_job(jid)
    assert job.status == "succeeded"
    assert job.finished_at is not None and job.finished_at.tzinfo is not None
    assert s.get_job(999) is None


def test_list_jobs_newest_first_and_limit(tmp_path):
    s = make(tmp_path)
    ids = [s.create_job("compose", f"t{i}", "u") for i in range(3)]
    assert [j.id for j in s.list_jobs()] == ids[::-1]
    assert [j.id for j in s.list_jobs(limit=2)] == ids[::-1][:2]


def test_mark_interrupted_jobs_only_running(tmp_path):
    s = make(tmp_path)
    running = s.create_job("compose", "a", "u")
    s.append_log(running, "partial\n")
    done = s.create_job("compose", "b", "u")
    s.finish_job(done, "succeeded")
    s.mark_interrupted_jobs()
    r = s.get_job(running)
    assert r.status == "failed"
    assert r.finished_at is not None
    assert r.log.startswith("partial\n")
    assert "Interrupted – the dashboard restarted" in r.log
    assert s.get_job(done).status == "succeeded"
    assert "Interrupted" not in s.get_job(done).log


def test_mark_interrupted_jobs_self_update_succeeds(tmp_path):
    s = make(tmp_path)
    me = s.create_job("self", "dashboard", "u")
    s.append_log(me, "Helper started\n")
    other = s.create_job("build", "x", "u")
    s.mark_interrupted_jobs()
    j = s.get_job(me)
    assert j.status == "succeeded"
    assert j.finished_at is not None
    assert j.log == "Helper started\nDashboard restarted – self-update applied\n"
    assert s.get_job(other).status == "failed"
    assert "Interrupted" in s.get_job(other).log


def test_check_results_pruned_to_latest_100(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = Store(str(tmp_path / "p.db"))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(105):
        store.save_check({"n": i}, start + timedelta(hours=i))
    (count,) = store._db.execute("SELECT COUNT(*) FROM check_results").fetchone()
    assert count == 100
    assert store.latest_check()[1] == {"n": 104}
    (oldest,) = store._db.execute("SELECT MIN(checked_at) FROM check_results").fetchone()
    assert oldest == (start + timedelta(hours=5)).isoformat()


def test_fresh_database_is_not_an_upgrade(tmp_path):
    s = Store(str(tmp_path / "d.db"))
    assert s.get_meta("upgraded_from_0_1") is None


def test_0_1_database_is_upgraded_and_keeps_jobs(tmp_path):
    path = str(tmp_path / "d.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
               "target TEXT NOT NULL, user TEXT NOT NULL, status TEXT NOT NULL, "
               "started_at TEXT, finished_at TEXT, log TEXT NOT NULL DEFAULT '')")
    db.execute("CREATE TABLE check_results (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "checked_at TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("INSERT INTO jobs (kind, target, user, status) VALUES ('build','web','me','succeeded')")
    db.commit(); db.close()
    s = Store(path)
    assert s.get_meta("upgraded_from_0_1") == "1"
    assert [j.target for j in s.list_jobs()] == ["web"]


def test_migrations_idempotent_on_restart(tmp_path):
    path = str(tmp_path / "d.db")
    Store(path).save_settings({"server_name": "a"})
    s = Store(path)
    assert s.load_settings() == {"server_name": "a"}
    assert s.get_meta("upgraded_from_0_1") is None


def test_settings_upsert_and_delete(tmp_path):
    s = Store(str(tmp_path / "d.db"))
    s.save_settings({"check_interval_hours": 6.0, "digest_to": ["a@x.com"]})
    s.save_settings({"check_interval_hours": 3.0})
    assert s.load_settings() == {"check_interval_hours": 3.0, "digest_to": ["a@x.com"]}
    s.delete_setting("digest_to")
    assert "digest_to" not in s.load_settings()


def test_audit_newest_first(tmp_path):
    s = Store(str(tmp_path / "d.db"))
    s.add_audit("a@x.com", "settings", "first"); s.add_audit("b@x.com", "settings", "second")
    assert [e.summary for e in s.list_audit()] == ["second", "first"]


def test_session_rows_and_purge(tmp_path):
    s = Store(str(tmp_path / "d.db"))
    old = datetime(2026, 1, 1, tzinfo=timezone.utc); new = datetime(2026, 10, 18, tzinfo=timezone.utc)
    s.session_put("h1", {"user": {"email": "a"}}, old, old)
    s.session_put("h2", {}, new, new)
    assert s.session_get("h1")[0] == {"user": {"email": "a"}}
    assert s.session_purge(idle_before=new, created_before=old) == 1
    assert s.session_get("h1") is None and s.session_get("h2") is not None


def test_session_touch_and_update_never_create_rows(tmp_path):
    from datetime import datetime, timezone
    s = Store(str(tmp_path / "d.db"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert s.session_touch("missing", now) is False
    assert s.session_update("missing", {"a": 1}, now) is False
    assert s.session_get("missing") is None
    s.session_put("h", {"a": 1}, now, now)
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert s.session_update("h", {"a": 2}, later) is True
    assert s.session_get("h") == ({"a": 2}, now, later)
    assert s.session_touch("h", now) is True
    assert s.session_get("h") == ({"a": 2}, now, now)


def _make_0_1(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
               "target TEXT NOT NULL, user TEXT NOT NULL, status TEXT NOT NULL, "
               "started_at TEXT, finished_at TEXT, log TEXT NOT NULL DEFAULT '')")
    db.commit(); db.close()


def test_upgrade_flag_survives_a_failed_migration(tmp_path, monkeypatch):
    import pytest

    from app import store as store_mod

    path = str(tmp_path / "d.db")
    _make_0_1(path)

    def boom(db):
        raise RuntimeError("migration 2 failed")

    monkeypatch.setattr(store_mod, "MIGRATIONS", [store_mod._migration_1, boom])
    with pytest.raises(RuntimeError):
        Store(path)
    monkeypatch.undo()
    assert Store(path).get_meta("upgraded_from_0_1") == "1"


def test_upgrade_flag_kept_across_restarts(tmp_path):
    path = str(tmp_path / "d.db")
    _make_0_1(path)
    assert Store(path).get_meta("upgraded_from_0_1") == "1"
    assert Store(path).get_meta("upgraded_from_0_1") == "1"
    assert Store(path).get_meta("upgraded_from_0_1") == "1"


def _make_0_2(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.execute("INSERT INTO schema_version VALUES (2)")
    db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
               "target TEXT NOT NULL, user TEXT NOT NULL, status TEXT NOT NULL, "
               "started_at TEXT, finished_at TEXT, log TEXT NOT NULL DEFAULT '')")
    db.execute("CREATE TABLE check_results (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "checked_at TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, data TEXT NOT NULL, "
               "created_at TEXT NOT NULL, last_seen TEXT NOT NULL)")
    db.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
               "user TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL)")
    db.execute("INSERT INTO sessions VALUES ('h', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    db.commit(); db.close()


def test_fresh_database_is_not_an_existing_install(tmp_path):
    assert Store(str(tmp_path / "d.db")).get_meta("existing_install") is None


def test_0_2_database_is_an_existing_install_without_upgrade_flag(tmp_path):
    path = str(tmp_path / "d.db")
    _make_0_2(path)
    s = Store(path)
    assert s.get_meta("existing_install") == "1"
    assert s.get_meta("upgraded_from_0_1") is None
    assert s.session_get("h") is not None


def test_0_1_database_is_an_existing_install(tmp_path):
    path = str(tmp_path / "d.db")
    _make_0_1(path)
    s = Store(path)
    assert s.get_meta("existing_install") == "1"
    assert s.get_meta("upgraded_from_0_1") == "1"


def test_session_user_id_and_delete_for_user(tmp_path):
    s = Store(str(tmp_path / "d.db"))
    now = datetime.now(timezone.utc)
    s.session_put("a", {}, now, now, user_id=1)
    s.session_put("b", {}, now, now)
    assert s.session_update("b", {"x": 1}, now, user_id=1) is True
    assert s.session_delete_for_user(1) == 2
    assert s.session_get("a") is None
