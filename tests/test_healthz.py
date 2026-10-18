"""/healthz: public liveness probe, its access-log filter, and the Dockerfile HEALTHCHECK."""
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import HealthzAccessFilter, create_app
from app.store import Store
from auth_helpers import assert_all_routes_guarded

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app(settings, tmp_path):
    app = create_app(
        settings,
        docker_client=object(),
        http=httpx.AsyncClient(),
        store=Store(str(tmp_path / "data" / "dashboard.db")),
        start_scheduler=False,
    )
    yield app
    app.state.store.close()


def test_healthz_is_public_and_exact(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        r = c.get("/healthz", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.text == '{"status":"ok"}'
    assert r.json() == {"status": "ok"}
    assert r.headers["cache-control"] == "no-store"


def test_healthz_head_works(app):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        r = c.head("/healthz")
    assert r.status_code == 200 and r.text == ""
    assert r.headers["cache-control"] == "no-store"


def test_healthz_503_when_store_fails(app):
    class Broken:
        def execute(self, *a, **kw):
            raise RuntimeError("database is locked: /secret/path")

        def close(self):
            pass

    app.state.store._db = Broken()
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        r = c.get("/healthz")
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}
    assert "secret" not in r.text and r.headers["cache-control"] == "no-store"


def test_healthz_reachable_while_setup_is_open(app):
    class Setup:
        @staticmethod
        def is_open():
            return True

    app.state.setup = Setup()
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        assert c.get("/healthz").status_code == 200


def test_route_guard_helper_accepts_healthz(app):
    assert_all_routes_guarded(app)


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", path, "1.1", 200), None,
    )


def test_access_log_filter_drops_only_healthz():
    f = HealthzAccessFilter()
    assert f.filter(_record("/healthz")) is False
    assert f.filter(_record("/healthz?x=1")) is False
    assert f.filter(_record("/")) is True
    assert f.filter(_record("/healthzz")) is True
    assert f.filter(logging.LogRecord("x", logging.INFO, __file__, 1, "plain", None, None)) is True


def test_dockerfile_healthcheck_targets_healthz():
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    checks = [line for line in lines if line.startswith("HEALTHCHECK")]
    assert len(checks) == 1
    check = checks[0]
    assert "--interval=30s --timeout=5s --start-period=20s --retries=3" in check
    assert "http://127.0.0.1:8000/healthz" in check
    assert check.split("CMD", 1)[1].lstrip().startswith('["python", "-c"')
