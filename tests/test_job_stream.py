"""An open job-log stream stops when the viewer loses access."""
import threading

import httpx
import pytest

from app import main
from app.main import create_app
from app.roles import Role
from app.store import Store
from auth_helpers import logged_in_client


@pytest.fixture
def app(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STREAM_RECHECK", 0.1)
    app = create_app(
        settings, docker_client=object(), http=httpx.AsyncClient(),
        store=Store(str(tmp_path / "data" / "dashboard.db")), start_scheduler=False,
    )
    yield app
    app.state.store.close()


def stream_ends_after(app, revoke) -> bool:
    """Open a stream on a job that never finishes, revoke access shortly after, and say
    whether the stream ended by itself."""
    job_id = app.state.store.create_job("build", "web", "someone@example.com")
    app.state.runner._subscribers[job_id] = []  # looks like it is still running
    done = threading.Event()

    def read():
        with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
            rec = app.state.users.by_email("local", "test@example.com")
            threading.Timer(0.4, revoke, args=(rec,)).start()
            with c.stream("GET", f"/jobs/{job_id}/stream") as r:
                for _ in r.iter_lines():
                    pass
        done.set()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return done.wait(10)


def test_stream_ends_when_the_user_is_disabled(app):
    assert stream_ends_after(app, lambda rec: app.state.users.update(rec.id, disabled=True))


def test_stream_ends_when_their_sessions_are_ended(app):
    assert stream_ends_after(app, lambda rec: app.state.users.end_sessions(rec.id))


def test_stream_ends_when_demoted_below_operator(app):
    def demote(rec):
        with app.state.users._store.write_tx() as db:
            db.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (rec.id,))
    assert stream_ends_after(app, demote)
