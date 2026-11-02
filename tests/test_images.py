from datetime import datetime, timezone

from app.checker import CheckResult, ContainerStatus, Status
from app.images import (
    ImageRow, ImagesReport, UnusedSummary, delete_images, human_size, protected_ids, scan,
)
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


# ---- delete_images ------------------------------------------------------------------


class LogCtx:
    def __init__(self):
        self.logs: list[str] = []

    def log(self, text):
        self.logs.append(text)


class RemovableImages:
    """A small Docker image store: tags point at ids; removing the last tag removes the
    image unless `keep_untagged`; `refuse` maps a ref to the error Docker gives for it."""

    def __init__(self, images, refuse=None, keep_untagged=()):
        self.images = {i.id: i for i in images}
        self.refuse = refuse or {}
        self.keep_untagged = set(keep_untagged)
        self.removed: list[str] = []

    def _find(self, ref):
        import docker.errors
        if ref in self.images:
            return self.images[ref]
        for image in self.images.values():
            if ref in image.attrs["RepoTags"]:
                return image
        raise docker.errors.ImageNotFound(f"no such image: {ref}")

    def get(self, ref):
        return self._find(ref)

    def remove(self, ref, force=False):
        assert force is False
        if ref in self.refuse:
            raise self.refuse[ref]
        image = self._find(ref)
        self.removed.append(ref)
        if ref == image.id:
            del self.images[image.id]
            return
        image.attrs["RepoTags"].remove(ref)
        if not image.attrs["RepoTags"] and image.id not in self.keep_untagged:
            del self.images[image.id]


def removable_client(images, used=(), **kw):
    client = FakeClient([], used=used)
    client.images = RemovableImages(images, **kw)
    return client


class RecordingStore:
    def __init__(self):
        self.forgotten: set[str] = set()

    def forget_unused_images(self, ids):
        self.forgotten |= set(ids)


async def test_delete_removes_each_tag_then_the_image():
    a = FakeImage("sha256:a", ["redis:7.0", "redis:7"], 2_000_000_000)
    b = FakeImage("sha256:0123456789abcdef", [], 500_000_000)
    c = FakeImage("sha256:c", ["nginx:1.25"], 300_000_000)
    client = removable_client([a, b, c], keep_untagged={"sha256:c"})
    ctx, store = LogCtx(), RecordingStore()
    await delete_images(ctx, client, store, ["sha256:a", "sha256:0123456789abcdef", "sha256:c"])
    # Tags first (Docker won't remove a multi-tagged image by id without force), then the
    # id only while the image is still there.
    assert client.images.removed == ["redis:7.0", "redis:7", "sha256:0123456789abcdef",
                                     "nginx:1.25", "sha256:c"]
    assert client.images.images == {}
    assert store.forgotten == {"sha256:a", "sha256:0123456789abcdef", "sha256:c"}
    assert ctx.logs == [
        "Deleted redis:7.0, redis:7 (2.0 GB)\n",
        "Deleted untagged 0123456789ab (500 MB)\n",
        "Deleted nginx:1.25 (300 MB)\n",
        "Freed 2.8 GB\n",
    ]


async def test_delete_skips_an_image_docker_refuses_and_continues():
    import docker.errors
    a = FakeImage("sha256:a", ["redis:7.0"], 1_000_000)
    b = FakeImage("sha256:b", ["nginx:1.25"], 2_000_000)
    in_use = docker.errors.APIError("409", explanation="image is being used by a container")
    client = removable_client([a, b], refuse={"redis:7.0": in_use})
    ctx, store = LogCtx(), RecordingStore()
    await delete_images(ctx, client, store, ["sha256:a", "sha256:b"])
    assert "sha256:a" in client.images.images and "sha256:b" not in client.images.images
    assert store.forgotten == {"sha256:b"}
    assert ctx.logs == [
        "Skipped redis:7.0 – image is being used by a container\n",
        "Deleted nginx:1.25 (2 MB)\n",
        "Freed 2 MB\n",
    ]


async def test_delete_notes_an_image_already_gone():
    client = removable_client([])
    ctx, store = LogCtx(), RecordingStore()
    await delete_images(ctx, client, store, ["sha256:0123456789abcdef"])
    assert ctx.logs == ["Already deleted 0123456789ab\n", "Freed 0 B\n"]
    assert store.forgotten == {"sha256:0123456789abcdef"}


async def test_delete_skips_an_image_a_container_started_using():
    # A container picked the image up after the page was shown: leave it, tags and all.
    a = FakeImage("sha256:a", ["redis:7.0", "redis:7"], 1_000_000)
    client = removable_client([a], used=["sha256:a"])
    ctx, store = LogCtx(), RecordingStore()
    await delete_images(ctx, client, store, ["sha256:a"])
    assert client.images.removed == []
    assert store.forgotten == set()
    assert ctx.logs == ["Skipped redis:7.0, redis:7 – a container uses it\n", "Freed 0 B\n"]


async def test_delete_leaves_a_tag_that_now_points_at_another_image():
    # nginx:latest was re-pulled since the scan: untagging it would strip the new image.
    old = FakeImage("sha256:old", ["nginx:latest"], 1_000_000)
    client = removable_client([old])
    new = FakeImage("sha256:new", ["nginx:latest"], 1_000_000)
    old.attrs["RepoTags"] = ["nginx:latest"]
    client.images.images = {"sha256:new": new, "sha256:old": old}  # tag lookup finds new first
    ctx, store = LogCtx(), RecordingStore()
    await delete_images(ctx, client, store, ["sha256:old"])
    assert client.images.removed == ["sha256:old"]
    assert "sha256:new" in client.images.images and new.attrs["RepoTags"] == ["nginx:latest"]
