from datetime import datetime, timezone

import pytest

from app import view
from app.checker import CheckResult, ContainerStatus, Status
from app.config import Settings
from app.inventory import ContainerInfo, Mount
from app.layout import LayoutProblem, layout_problems
from app.rules import Rules

MAILCOW = "/opt/mailcow-dockerized"


def make(name="web", project="site", mounts=(), **kw):
    values = dict(
        id=name.ljust(64, "0"), name=name, image_ref="nginx", image_id="sha256:i",
        running_repo_digests=[], local_tag_image_id=None, project=project,
        working_dir="/docker/site", config_files=["/docker/site/compose.yml"], service=name,
        state="running", is_self=False, mounts=list(mounts),
    )
    values.update(kw)
    return ContainerInfo(**values)


def bind(src, dst="/x", ro=False):
    return Mount("bind", src, dst, ro)


def volume(name, dst="/x"):
    return Mount("volume", f"/var/lib/docker/volumes/{name}/_data", dst, False, name)


def run(containers, rules=None, **setting_kw):
    settings = Settings(_env_file=None, **setting_kw)
    return layout_problems(containers, settings, lambda c: rules or Rules(), "/data", MAILCOW)


def kinds(problems):
    return [p.kind for p in problems]


def test_bind_outside_flagged_with_hint():
    [p] = run([make(mounts=[bind("/srv/app/data", "/data")])])
    assert p.kind == "bind_outside" and p.severity == "problem" and p.project == "site"
    assert p.key == "site/web:bind_outside:/srv/app/data:/data"
    assert p.hint == ("Move the data to `/docker/site/data` and change `/srv/app/data:/data` "
                      "to `./data:/data`.")


def test_named_volume_flagged_with_hint():
    [p] = run([make(mounts=[volume("pgdata", "/var/lib/postgresql/data")])])
    assert p.kind == "named_volume" and p.key == "site/web:named_volume:pgdata:/var/lib/postgresql/data"
    assert p.hint.startswith("Move the data to `/docker/site/data` and change "
                             "`pgdata:/var/lib/postgresql/data` to `./data:/var/lib/postgresql/data`.")
    assert "driver_opts" in p.hint and "layout.ignore" in p.hint


def test_compose_and_env_files_outside_flagged():
    c = make(config_files=["/root/app/compose.yml"], env_files=["/etc/app.env"],
             working_dir="/root/app")
    assert kinds(run([c])) == ["compose_outside", "env_outside"]


def test_not_compose_is_a_notice():
    [p] = run([make("lone", project=None, working_dir=None, config_files=[], service=None)])
    assert p.kind == "not_compose" and p.severity == "notice" and p.project is None
    assert p.key == "lone:not_compose:-"


@pytest.mark.parametrize("m", [
    bind("/var/run/docker.sock"), bind("/run/docker.sock"), bind("/etc/localtime", ro=True),
    bind("/etc/timezone", ro=True), bind("/dev/ttyUSB0"), bind("/dev"), bind("/proc", ro=True),
    bind("/sys", ro=True), bind("/sys/fs/cgroup", ro=True),
    Mount("tmpfs", "", "/tmp", False), volume("a" * 64),
    Mount("volume", "/var/lib/docker/volumes/" + "b" * 64 + "/_data", "/v", False, "b" * 64),
    bind("/docker/site/data"), bind("/docker"), bind(MAILCOW + "/data"),
])
def test_allowed_mounts_not_flagged(m):
    assert run([make(mounts=[m])]) == []


def test_sys_read_write_flagged():
    assert kinds(run([make(mounts=[bind("/sys", ro=False)])])) == ["bind_outside"]


def test_prefix_lookalike_and_dotdot_flagged():
    assert kinds(run([make(mounts=[bind("/dockerx/data"), bind("/docker/../srv/data"),
                                   bind("/docker/site/../../srv")])])) == ["bind_outside"] * 3
    assert run([make(mounts=[bind("/docker//site/./data")])]) == []


def test_compose_root_setting_is_used():
    assert kinds(run([make(mounts=[bind("/docker/a")])], compose_root="/srv/stacks")) == [
        "compose_outside", "bind_outside"]


def test_own_data_dir_allowed_but_other_mounts_not():
    me = make("dash", is_self=True, mounts=[bind("/home/me/dm", "/data"),
                                            volume("dmdata", "/data"), bind("/srv/other", "/o")])
    assert [p.detail.split()[-1] for p in run([me])] == ["/o"]
    other = make("web", mounts=[bind("/home/me/dm", "/data")])
    assert kinds(run([other])) == ["bind_outside"]


def test_mailcow_never_flagged():
    mc = make("mc", project="mailcowdockerized", working_dir=MAILCOW, config_files=[],
              mounts=[bind("/srv/x"), volume("vmail")])
    by_dir = make("mc2", project="other", working_dir=MAILCOW, mounts=[bind("/srv/x")])
    assert run([mc, by_dir]) == []


def test_allowlist_ignore_label_and_named_volumes_allowed():
    c = make(mounts=[bind("/srv/media/tv"), volume("pgdata")])
    assert kinds(run([c], layout_allowlist=["/srv/media"])) == ["named_volume"]
    assert kinds(run([c], named_volumes_allowed=True)) == ["bind_outside"]
    assert run([c], rules=Rules(layout_ignore=True)) == []
    assert kinds(run([c], layout_allowlist=["/srv/med"])) == ["bind_outside", "named_volume"]


def test_layout_check_off_gives_nothing():
    assert run([make(mounts=[bind("/srv/x")])], layout_check=False) == []


def test_keys_are_stable_and_unique():
    c = make(mounts=[bind("/srv/a"), bind("/srv/b"), volume("v")])
    a, b = run([c]), run([c])
    assert [p.key for p in a] == [p.key for p in b] and len({p.key for p in a}) == 3


def test_result_round_trips_and_old_results_load():
    p = LayoutProblem("k", "web", "site", "bind_outside", "d", "h", "problem")
    r = CheckResult(datetime(2026, 10, 18, tzinfo=timezone.utc), [], None, [p])
    assert CheckResult.from_dict(r.to_dict()).layout == [p]
    old = r.to_dict()
    del old["layout"]
    assert CheckResult.from_dict(old).layout is None


def test_view_counts_and_anchors():
    c = make(mounts=[bind("/srv/a"), volume("v")])
    r = CheckResult(datetime(2026, 10, 18, tzinfo=timezone.utc),
                    [ContainerStatus(c, Status.UP_TO_DATE)], None, run([c]))
    assert view.layout_problems(r, r.containers[0]) == 2
    assert view.layout_mounts(r, r.containers[0]) == 2
    assert view.layout_anchor("My Site/<x>") == "my-site-x"
    assert view.layout_anchor(None) != view.layout_anchor("no-project")


# --- page and dashboard -------------------------------------------------------------------

from test_routes import CHECKED_AT, make_app  # noqa: E402
from auth_helpers import logged_in_client  # noqa: E402


def _client(settings, tmp_path, problems):
    c = make(mounts=[bind("/srv/a", "/a")])
    r = CheckResult(CHECKED_AT, [ContainerStatus(c, Status.UP_TO_DATE)], None, problems(c))
    return logged_in_client(make_app(settings, tmp_path, r), follow_redirects=False)


def test_layout_page_groups_by_project_and_dashboard_links_to_it(settings, tmp_path):
    with _client(settings, tmp_path, lambda c: run([c])) as client:
        page = client.get("/layout").text
        assert 'id="site"' in page and "Move the data to" in page
        dash = client.get("/").text
        assert 'data-layout="1"' in dash
        assert 'href="/layout#site"' in dash and "1 mount outside /docker" in dash


def test_layout_page_empty_and_disabled(settings, tmp_path):
    with _client(settings, tmp_path, lambda c: []) as client:
        assert "Nothing stored outside /docker" in client.get("/layout").text
    c = make(mounts=[bind("/srv/a")])
    r = CheckResult(CHECKED_AT, [ContainerStatus(c, Status.UP_TO_DATE)], None, run([c]))
    app = make_app(settings, tmp_path / "b", r)
    app.state.settings = app.state.settings.model_copy(update={"layout_check": False})
    with logged_in_client(app, follow_redirects=False) as client:
        page = client.get("/layout").text
        assert "switched off" in page and "Move the data" not in page


async def test_unique_keys_per_destination_and_mailcow_volume_allowed():
    c = make(mounts=[bind("/srv/a", "/one"), bind("/srv/a", "/two"), volume("mailcowdockerized_x")])
    assert len({p.key for p in run([c])}) == 2


@pytest.mark.parametrize("src", ["/", "/run/user/1000/docker.sock", "/etc/ssl/certs", "C:\\data",
                                 "/var/lib/docker/volumes/x", "/boot"])
def test_system_paths_get_the_system_hint(src):
    [p] = run([make(mounts=[bind(src, "/host", ro=True)])])
    assert p.hint.startswith("System path") and "Settings → Checking" in p.hint


def test_root_compose_root_has_no_double_slash():
    ps = run([make(mounts=[volume("v")]), make("lone", project=None, config_files=[])],
             compose_root="/")
    assert len(ps) == 2 and all("//" not in p.hint for p in ps)


def test_hints_render_code_and_escape(settings, tmp_path):
    from app.view import hint_parts
    assert hint_parts("a `b` c") == [(False, "a "), (True, "b"), (False, " c")]
    c = make(mounts=[bind("/srv/<script>", "/a")])
    r = CheckResult(CHECKED_AT, [ContainerStatus(c, Status.UP_TO_DATE)], None, run([c]))
    with logged_in_client(make_app(settings, tmp_path, r), follow_redirects=False) as client:
        page = client.get("/layout").text
        assert "<code>" in page and "/srv/<script>" not in page and "&lt;script&gt;" in page


def test_no_layout_data_is_not_all_clear(settings, tmp_path):
    c = make()
    for layout in (None,):
        r = CheckResult(CHECKED_AT, [ContainerStatus(c, Status.UP_TO_DATE)], None, layout)
        with logged_in_client(make_app(settings, tmp_path, r), follow_redirects=False) as client:
            page = client.get("/layout").text
            assert "No layout check has run yet" in page and "Nothing stored" not in page


def test_dashboard_hides_layout_when_off(settings, tmp_path):
    c = make(mounts=[bind("/srv/a")])
    r = CheckResult(CHECKED_AT, [ContainerStatus(c, Status.UP_TO_DATE)], None, run([c]))
    app = make_app(settings, tmp_path, r)
    with logged_in_client(app, follow_redirects=False) as client:
        assert 'data-chip="layout"' in client.get("/").text
        app.state.settings = app.state.settings.model_copy(update={"layout_check": False})
        page = client.get("/").text
        assert 'data-chip="layout"' not in page and "/layout#" not in page
        assert 'data-layout="0"' in page


async def test_run_check_off_stores_none(settings, tmp_path, monkeypatch):
    from app.main import run_check
    from test_routes import StubDocker
    app = make_app(settings, tmp_path)
    app.state.settings = app.state.settings.model_copy(update={"layout_check": False})
    app.state.docker = StubDocker()
    c = make(mounts=[bind("/srv/a")])

    async def fake_check_all(containers, *a, **kw):
        return CheckResult(CHECKED_AT, [ContainerStatus(c, Status.UP_TO_DATE)])

    monkeypatch.setattr("app.main.check_all", fake_check_all)
    monkeypatch.setattr("app.main.list_containers", lambda docker, self_id: [c])
    assert (await run_check(app)).layout is None


async def test_run_check_stores_layout(settings, tmp_path, monkeypatch):
    from app.main import run_check
    from test_routes import StubDocker
    app = make_app(settings, tmp_path)
    c = make(mounts=[bind("/srv/a", "/a")])
    app.state.docker = StubDocker()

    async def fake_check_all(containers, *a, **kw):
        return CheckResult(CHECKED_AT, [ContainerStatus(c, Status.UP_TO_DATE)])

    monkeypatch.setattr("app.main.check_all", fake_check_all)
    monkeypatch.setattr("app.main.list_containers", lambda docker, self_id: [c])
    result = await run_check(app)
    assert [p.kind for p in result.layout] == ["bind_outside"]
    stored = CheckResult.from_dict(app.state.store.latest_check()[1])
    assert stored.layout == result.layout
