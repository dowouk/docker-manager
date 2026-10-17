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
