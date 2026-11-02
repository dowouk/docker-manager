from datetime import datetime

from app.checker import CheckResult, ContainerStatus, Status
from app.inventory import ContainerInfo
from app.prefetch import Target, compose_editable, targets


def make_info(**kw) -> ContainerInfo:
    base = dict(
        id="abc", name="web", image_ref="nginx:1.0", image_id="sha256:x",
        running_repo_digests=[], local_tag_image_id=None, project="proj",
        working_dir="/docker/proj", config_files=["/docker/proj/a.yml"],
        service="web", state="running", is_self=False,
    )
    base.update(kw)
    return ContainerInfo(**base)


def cs_for(info, status=Status.NEWER_BUILD, **kw) -> ContainerStatus:
    kw.setdefault("remote_digest", "sha256:new" if status == Status.NEWER_BUILD else None)
    return ContainerStatus(container=info, status=status, **kw)


def result(*statuses) -> CheckResult:
    return CheckResult(checked_at=datetime(2026, 11, 2), containers=list(statuses))


def yes(cs, tag):
    return True


def no(cs, tag):
    return False


def test_newer_build_is_a_target():
    info = make_info()
    out = targets(result(cs_for(info)), [info], yes)
    assert out == [Target(key="nginx:1.0", kind="build", info=info, ref="nginx:1.0", remote_digest="sha256:new")]


def test_shared_tag_with_skipped_sibling_is_not_a_target():
    a, b = make_info(id="a"), make_info(id="b", name="web2", service="web2")
    visible = result(cs_for(a), cs_for(b, Status.UP_TO_DATE))  # b's update was skipped
    assert targets(visible, [a, b], yes) == []


def test_shared_tag_with_unwatched_sibling_is_not_a_target():
    a, b = make_info(id="a"), make_info(id="b", name="web2", service="web2")
    visible = result(cs_for(a), cs_for(b, Status.NOT_WATCHED))
    assert targets(visible, [a, b], yes) == []


def test_shared_tag_with_mailcow_container_is_not_a_target():
    a = make_info(id="a")
    mail = make_info(id="m", name="mailcow-web", project="mailcowdockerized", service="nginx-mailcow")
    assert targets(result(cs_for(a)), [a, mail], yes) == []


def test_shared_tag_all_watched_gives_one_target():
    a, b = make_info(id="a"), make_info(id="b", name="web2", service="web2")
    out = targets(result(cs_for(a), cs_for(b)), [a, b], yes)
    assert len(out) == 1
    assert out[0].info is a and out[0].kind == "build"


def test_minor_version_is_a_target_with_new_ref():
    info = make_info(image_ref="postgres:16.2")
    out = targets(result(cs_for(info, Status.UP_TO_DATE, minor_update="16.4")), [info], yes)
    assert out == [Target(key="postgres:16.4", kind="version", info=info, ref="postgres:16.4", remote_digest=None)]


def test_minor_version_shared_by_two_containers_gives_one_target():
    a = make_info(id="a", image_ref="postgres:16.2")
    b = make_info(id="b", image_ref="postgres:16.2", service="db2")
    visible = result(
        cs_for(a, Status.UP_TO_DATE, minor_update="16.4"),
        cs_for(b, Status.UP_TO_DATE, minor_update="16.4"),
    )
    assert len(targets(visible, [a, b], yes)) == 1


def test_version_set_by_variable_is_not_a_target():
    info = make_info(image_ref="postgres:16.2")
    visible = result(cs_for(info, Status.UP_TO_DATE, minor_update="16.4"))
    assert targets(visible, [info], no) == []


def test_major_only_dashboard_and_non_compose_are_never_targets():
    major = make_info(id="1", image_ref="redis:6.0", name="r")
    me = make_info(id="2", image_ref="app:1", name="me", is_self=True)
    plain = make_info(id="3", image_ref="x:1", name="x", service=None)
    manual = make_info(id="4", image_ref="y:1", name="y")
    visible = result(
        cs_for(major, Status.UP_TO_DATE, major_update="7.0"),
        cs_for(me),
        cs_for(plain),
        cs_for(manual, updatable=False, minor_update="1.2"),
    )
    assert targets(visible, [major, me, plain, manual], yes) == []


def test_compose_editable_literal_tag_yes_variable_no(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text("services:\n  db:\n    image: postgres:16.2\n  cache:\n    image: redis:${REDIS_TAG}\n")
    db = cs_for(make_info(image_ref="postgres:16.2", service="db", config_files=[str(f)]), Status.UP_TO_DATE)
    cache = cs_for(make_info(image_ref="redis:7.0", service="cache", config_files=[str(f)]), Status.UP_TO_DATE)
    assert compose_editable(db, "16.4") is True
    assert compose_editable(cache, "7.2") is False


def test_compose_editable_missing_file_is_false(tmp_path):
    cs = cs_for(make_info(config_files=[str(tmp_path / "gone.yml")]), Status.UP_TO_DATE)
    assert compose_editable(cs, "1.1") is False
