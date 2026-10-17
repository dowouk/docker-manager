from dataclasses import dataclass

DOCKER_HUB = "docker.io"
DOCKER_HUB_API_HOST = "registry-1.docker.io"


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str | None
    digest: str | None
    # Image name exactly as written by the user, without tag or digest.
    original_name: str = ""

    @property
    def api_host(self) -> str:
        return DOCKER_HUB_API_HOST if self.is_docker_hub else self.registry

    @property
    def is_docker_hub(self) -> bool:
        return self.registry == DOCKER_HUB

    def with_tag(self, tag: str) -> str:
        return f"{self.original_name}:{tag}"


def parse_image_ref(ref: str) -> ImageRef:
    rest = ref.strip()
    digest = None
    if "@" in rest:
        rest, digest = rest.split("@", 1)
    tag = None
    last_slash = rest.rfind("/")
    colon = rest.rfind(":")
    if colon > last_slash:
        rest, tag = rest[:colon], rest[colon + 1 :]
    original_name = rest

    registry = DOCKER_HUB
    repository = rest
    first, sep, remainder = rest.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, remainder
    if registry == DOCKER_HUB and "/" not in repository:
        repository = f"library/{repository}"

    if tag is None and digest is None:
        tag = "latest"
    return ImageRef(registry, repository, tag, digest, original_name)
