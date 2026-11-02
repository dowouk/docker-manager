from datetime import datetime, timezone

from app.checker import CheckResult, ContainerStatus, Status
from app.images import ImageRow, ImagesReport, UnusedSummary, human_size, protected_ids, scan
from app.store import Store
from test_routes import CHECKED_AT, info

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class FakeImage:
    def __init__(self, image_id, tags=(), size=1000):
        self.id = image_id
        self.attrs = {"RepoTags": list(tags), "Size": size}


class FakeContainer:
    def __init__(self, image_id):
        self.attrs = {"Image": image_id}


class FakeImages:
    def __init__(self, items):
        self.items = items

    def list(self):
        return self.items


class FakeContainers:
    def __init__(self, items):
        self.items = items

    def list(self, all=False):
        assert all is True
        return self.items


class FakeClient:
    def __init__(self, images, used=()):
        self.images = FakeImages(list(images))
        self.containers = FakeContainers([FakeContainer(i) for i in used])


def result_of(*statuses):
    return CheckResult(CHECKED_AT, list(statuses))


def lookup(mapping):
    return lambda ref: mapping.get(ref)


def test_scan_lists_images_no_container_uses(tmp_path):
    a = FakeImage("sha256:a", ["nginx:latest"], 300)
    b = FakeImage("sha256:b", ["redis:7.0"], 500)
    c = FakeImage("sha256:0123456789abcdef", [], 100)
    client = FakeClient([a, b, c], used=["sha256:a"])  # a: used by a stopped container
    report = scan(client, Store(str(tmp_path / "s.db")), None, NOW)
    assert [r.id for r in report.unused] == ["sha256:b", "sha256:0123456789abcdef"]
    assert report.unused[1].name == "untagged 0123456789ab"
    assert report.unused[0].name == "redis:7.0"
    assert report.waiting == []
    assert report.unused[0].unused_since == NOW and report.unused[0].before is True


def test_scan_hides_none_none_tag(tmp_path):
    img = FakeImage("sha256:a", ["<none>:<none>"], 10)
    report = scan(FakeClient([img]), Store(str(tmp_path / "s.db")), None, NOW)
    assert report.unused[0].tags == []


class DockerImages(FakeImages):
    def __init__(self, items, known):
        super().__init__(items)
        self.known = known

    def get(self, ref):
        import docker.errors
        if ref not in self.known:
            raise docker.errors.ImageNotFound("no")
        return FakeImage(self.known[ref])


def test_protected_build_download_is_waiting_not_unused(tmp_path):
    old = FakeImage("sha256:old", ["nginx:latest"], 100)
    new = FakeImage("sha256:new", [], 900)
    client = FakeClient([old, new], used=["sha256:old"])
    client.images = DockerImages([old, new], {"nginx:latest": "sha256:new"})
    web = info(tmp_path, "web", image_id="sha256:old", local_tag_image_id="sha256:new")
    # Stored status is still NEWER_BUILD although the download finished since the check.
    for st in (Status.PULLED_NOT_RECREATED, Status.NEWER_BUILD):
        report = scan(client, Store(str(tmp_path / f"{st}.db")), result_of(ContainerStatus(web, st)), NOW)
        assert [r.id for r in report.waiting] == ["sha256:new"]
        assert report.unused == []


def test_build_ref_resolving_to_running_image_is_not_protected(tmp_path):
    web = info(tmp_path, "web", image_id="sha256:old")
    visible = result_of(ContainerStatus(web, Status.NEWER_BUILD))
    assert protected_ids(visible, lookup({"nginx:latest": "sha256:old"})) == set()
    assert protected_ids(visible, lookup({})) == set()


def test_protected_version_download_is_waiting(tmp_path):
    old = FakeImage("sha256:v162", ["postgres:16.2"], 100)
    new = FakeImage("sha256:v164", ["postgres:16.4"], 800)
    client = FakeClient([old, new], used=["sha256:v162"])
    db = info(tmp_path, "db", image="postgres:16.2", image_id="sha256:v162")
    visible = result_of(ContainerStatus(db, Status.UP_TO_DATE, minor_update="16.4"))
    assert protected_ids(visible, lookup({"postgres:16.4": "sha256:v164"})) == {"sha256:v164"}
    client.images = DockerImages([old, new], {"postgres:16.4": "sha256:v164"})
    report = scan(client, Store(str(tmp_path / "s.db")), visible, NOW)
    assert [r.id for r in report.waiting] == ["sha256:v164"]
    assert report.unused == []


def test_skipped_update_is_not_protected(tmp_path):
    new = FakeImage("sha256:v164", ["postgres:16.4"], 800)
    client = FakeClient([new])
    client.images = DockerImages([new], {"postgres:16.4": "sha256:v164"})
    db = info(tmp_path, "db", image="postgres:16.2", image_id="sha256:v162")
    skipped = result_of(ContainerStatus(db, Status.UP_TO_DATE, minor_update=None))
    report = scan(client, Store(str(tmp_path / "s.db")), skipped, NOW)
    assert [r.id for r in report.unused] == ["sha256:v164"]
    assert report.waiting == []


def test_scan_records_unused_since_in_the_store(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    client = FakeClient([FakeImage("sha256:b", ["redis:7"], 5)])
    scan(client, store, None, NOW)
    later = datetime(2026, 11, 3, tzinfo=timezone.utc)
    row = scan(client, store, None, later).unused[0]
    assert row.unused_since == NOW and row.before is True


def test_summary_counts_unused_only():
    unused = [ImageRow("sha256:a", [], 300, NOW, False), ImageRow("sha256:b", [], 200, NOW, False)]
    waiting = [ImageRow("sha256:c", [], 999, NOW, False)]
    assert ImagesReport(unused, waiting).summary() == UnusedSummary(2, 500)


def test_human_size():
    assert human_size(412_000_000) == "412 MB"
    assert human_size(6_400_000_000) == "6.4 GB"
    assert human_size(0) == "0 B"
