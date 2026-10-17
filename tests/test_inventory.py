import docker.errors

from app.inventory import detect_self_id, list_containers

ID_A = "a" * 64
ID_B = "b" * 64
ID_C = "c" * 64


class StubContainer:
    def __init__(self, cid, name, image_ref, image_id, state="running", labels=None):
        self.id = cid
        self.name = name
        self.labels = labels or {}
        self.status = state
        self.attrs = {
            "Config": {"Image": image_ref, "Labels": self.labels},
            "Image": image_id,
            "State": {"Status": state},
        }


class StubImage:
    def __init__(self, image_id, digests):
        self.id = image_id
        self.attrs = {"RepoDigests": digests}


class StubContainers:
    def __init__(self, items):
        self.items = items
        self.all_arg = None

    def list(self, all=False):
        self.all_arg = all
        return self.items


class StubImages:
    def __init__(self, images, errors=None):
        self.images = images
        self.errors = errors or {}

    def get(self, ref):
        if ref in self.errors:
            raise self.errors[ref]
        if ref not in self.images:
            raise docker.errors.ImageNotFound(f"no such image: {ref}")
        return self.images[ref]


class StubClient:
    def __init__(self, containers, images):
        self.containers = StubContainers(containers)
        self.images = images


def make_client():
    web_img = StubImage("sha256:web", ["nginx@sha256:aaa"])
    images = StubImages({"sha256:web": web_img, "nginx:1.25": StubImage("sha256:newweb", ["nginx@sha256:bbb"])})
    containers = [
        StubContainer(
            ID_A, "web", "nginx:1.25", "sha256:web",
            labels={
                "com.docker.compose.project": "site",
                "com.docker.compose.project.working_dir": "/docker/site",
                "com.docker.compose.project.config_files":
                    "/docker/site/compose.yml, /docker/site/compose.override.yml,",
                "com.docker.compose.service": "web",
            },
        ),
        StubContainer(ID_B, "plain", "redis:7", "sha256:gone", state="exited"),
    ]
    return StubClient(containers, images)


def test_compose_container_parsed():
    client = make_client()
    web = list_containers(client, None)[0]
    assert client.containers.all_arg is True
    assert web.id == ID_A
    assert web.name == "web"
    assert web.image_ref == "nginx:1.25"
    assert web.image_id == "sha256:web"
    assert web.running_repo_digests == ["nginx@sha256:aaa"]
    assert web.local_tag_image_id == "sha256:newweb"
    assert web.local_tag_repo_digests == ["nginx@sha256:bbb"]
    assert web.project == "site"
    assert web.working_dir == "/docker/site"
    assert web.config_files == ["/docker/site/compose.yml", "/docker/site/compose.override.yml"]
    assert web.service == "web"
    assert web.state == "running"
    assert web.is_self is False


def test_non_compose_exited_and_image_removed():
    plain = list_containers(make_client(), None)[1]
    assert plain.project is None
    assert plain.working_dir is None
    assert plain.service is None
    assert plain.config_files == []
    assert plain.state == "exited"
    assert plain.running_repo_digests == []
    assert plain.local_tag_image_id is None
    assert plain.local_tag_repo_digests == []


def test_api_error_on_image_lookup_does_not_crash():
    client = make_client()
    client.images = StubImages({}, errors={"sha256:web": docker.errors.APIError("boom")})
    web = list_containers(client, None)[0]
    assert web.running_repo_digests == []


def test_self_detection_by_prefix():
    result = list_containers(make_client(), ID_B[:12])
    assert [c.is_self for c in result] == [False, True]


def test_short_or_empty_self_id_never_matches():
    assert not any(c.is_self for c in list_containers(make_client(), "bbb"))
    assert not any(c.is_self for c in list_containers(make_client(), ""))


def test_detect_self_id(monkeypatch):
    monkeypatch.setenv("HOSTNAME", ID_C[:12])
    assert detect_self_id() == ID_C[:12]
    monkeypatch.delenv("HOSTNAME")
    assert detect_self_id() is None


def test_env_files_from_compose_label():
    labels = {
        "com.docker.compose.project": "site",
        "com.docker.compose.project.environment_file": "/docker/site/.env, /docker/site/extra.env",
    }
    c = StubContainer(ID_A, "web", "nginx:1.25", "sha256:web", labels=labels)
    (info,) = list_containers(StubClient([c], StubImages({})), None)
    assert info.env_files == ["/docker/site/.env", "/docker/site/extra.env"]
    plain = StubContainer(ID_B, "db", "postgres:16", "sha256:db")
    (info,) = list_containers(StubClient([plain], StubImages({})), None)
    assert info.env_files == []
