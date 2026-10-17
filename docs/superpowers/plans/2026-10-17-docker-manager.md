# Docker Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single-container web dashboard, run on each VPS, that shows whether every container's image is current, updates containers (and Mailcow) with one click, and emails a weekly digest.

**Architecture:** FastAPI app with server-rendered Jinja2 pages and a little vanilla JS. Pure-logic modules (image refs, version tags, compose editing) sit underneath I/O modules (registry client, Docker inventory, job runner, Mailcow runner), which a checker and update actions combine. State lives in SQLite on a `/data` volume. Entra ID sign-in guards every route.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Jinja2, httpx, Authlib, docker SDK, APScheduler 3.x, azure-communication-email, SQLite (stdlib `sqlite3`), pytest + pytest-asyncio + respx.

**Spec:** `docs/superpowers/specs/2026-10-17-docker-manager-design.md`

## Global Constraints

- UK English in all UI text, emails and README; use en dashes (–), never em dashes.
- Every route requires an authenticated Entra session except `/auth/login`, `/auth/callback` and `/static/*`. No unauthenticated fallback, setup mode or default password.
- Configuration only via env vars (table in spec); no config UI.
- Docker Hub: manifest checks use `HEAD` only; tag lists cached ≥ 24 h with a page cap; no Docker Hub credentials are recommended or required – everything works anonymously.
- Compose files are edited by targeted text replacement only – never YAML load/dump. Always back up first to `/data/backups/<project>/<filename>.YYYYMMDD-HHMMSS`.
- Pre-release markers excluded from version candidates (case-insensitive): `rc`, `beta`, `alpha`, `dev`, `nightly`, `preview`, `snapshot`.
- Only one update job runs at a time per server; a second request while busy is refused with "Another update is running – try again when it finishes".
- Mailcow: `update.sh --check` exit 0 = update available, 3 = none, other = error. Update = `./update.sh --force`, stdin closed, no TTY, re-run once on exit 2. Run on the host via `nsenter` with `env -i` and `bash -lc`.
- Defaults: `CHECK_INTERVAL_HOURS=6`, `COMPOSE_ROOT=/docker`, `MAILCOW_DIR=/opt/mailcow-dockerized`, `DIGEST_SCHEDULE="0 8 * * MON"`, `DIGEST_WHEN_NONE=true`, `TZ=Europe/London`, session lifetime 8 h.
- Image published to `ghcr.io/<owner>/docker-manager`, multi-arch amd64 + arm64, built with the built-in `GITHUB_TOKEN`; public repo, no secrets in it.

## Review Focus

1. **One registry fails or times out** – the other images must still be checked; only the affected rows show an error with the reason. (Test in Task 7.)
2. **Docker Hub rate limit (HTTP 429 on check, `toomanyrequests` in pull output)** – the user sees "Docker Hub rate limit reached – try again later", not a generic failure. (Tests in Tasks 4 and 10.)
3. **Real-world compose formatting** – quoted image values, CRLF line endings, comments after the value, a second service using the same image – the edit changes exactly one line and nothing else. (Tests in Task 8.)
4. **Containers in odd states** – exited containers, a container whose image has been removed locally, locally built images with no `RepoDigests` – the dashboard still loads and shows them sensibly. (Tests in Tasks 6 and 7.)
5. **Browser closed or refreshed during an update** – the job keeps running, the log is kept, and re-opening the job page replays the log so far and continues streaming. (Test in Task 9.)

---

## File Structure

```
pyproject.toml                 deps, pytest config
Dockerfile
.github/workflows/build.yml
README.md
examples/caddy/compose.yml, examples/caddy/Caddyfile
examples/traefik/compose.yml
examples/mailcow/compose.yml
app/
  config.py           Settings (env vars)
  imageref.py         parse image references
  versions.py         version-tag parsing and update selection
  registry.py         OCI registry client (digests, tags, auth, cache)
  inventory.py        list containers from Docker
  store.py            SQLite persistence (check results, jobs, logs)
  checker.py          combine inventory + registry + mailcow into statuses
  compose_edit.py     find/replace image tag in compose files, backup/restore
  jobs.py             one-at-a-time job runner with streamed logs
  updaters.py         compose update actions, health wait, self-update
  mailcow.py          host command, check, update
  auth.py             Entra OIDC, session guard, CSRF
  digest.py           render + send weekly email
  scheduler.py        periodic check + digest
  main.py             FastAPI app factory and routes
  templates/          base.html, dashboard.html, job.html, history.html, digest.html, digest.txt
  static/             app.css, app.js
tests/                one test file per module, conftest.py
```

---

### Task 1: Project scaffold and settings

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/config.py`, `tests/conftest.py`, `tests/test_config.py`, `.gitignore`

**Interfaces:**
- Produces: `class Settings(pydantic_settings.BaseSettings)` with fields `server_name: str`, `public_url: str`, `azure_tenant_id: str`, `azure_client_id: str`, `azure_client_secret: str`, `session_secret: str`, `allowed_group_id: str | None = None`, `allowed_emails: list[str] = []` (comma-separated env), `check_interval_hours: float = 6`, `compose_root: str = "/docker"`, `mailcow_dir: str = "/opt/mailcow-dockerized"`, `acs_connection_string: str | None = None`, `digest_from: str | None = None`, `digest_to: list[str] = []` (comma-separated), `digest_schedule: str = "0 8 * * MON"`, `digest_when_none: bool = True`, `tz: str = "Europe/London"`, `docker_config: str | None = None`, `data_dir: str = "/data"`. Property `digest_enabled -> bool` (all three ACS fields set). `get_settings() -> Settings` (cached).
- `tests/conftest.py` fixture `settings(tmp_path, monkeypatch) -> Settings` setting the required env vars and `DATA_DIR=tmp_path`.

- [ ] **Step 1: Write `pyproject.toml`** – dependencies: `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `jinja2>=3.1`, `httpx>=0.27`, `authlib>=1.3`, `itsdangerous>=2.2`, `docker>=7.1`, `apscheduler>=3.10,<4`, `azure-communication-email>=1.0`, `pydantic-settings>=2.4`, `python-multipart>=0.0.9`; dev: `pytest>=8`, `pytest-asyncio>=0.24`, `respx>=0.21`. `[tool.pytest.ini_options] asyncio_mode = "auto"`.

- [ ] **Step 2: Write failing tests**

```python
def test_defaults(settings):
    assert settings.check_interval_hours == 6
    assert settings.compose_root == "/docker"
    assert settings.mailcow_dir == "/opt/mailcow-dockerized"
    assert settings.digest_schedule == "0 8 * * MON"
    assert settings.tz == "Europe/London"
    assert settings.digest_enabled is False

def test_comma_lists(monkeypatch, settings):
    monkeypatch.setenv("DIGEST_TO", "a@x.com, b@x.com")
    monkeypatch.setenv("ALLOWED_EMAILS", "me@x.com")
    s = Settings()
    assert s.digest_to == ["a@x.com", "b@x.com"]
    assert s.allowed_emails == ["me@x.com"]

def test_missing_required_fails(monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
```

- [ ] **Step 3: Run** `pytest tests/test_config.py -v` – expect FAIL (import error).
- [ ] **Step 4: Implement `app/config.py`.** Comma lists via a `field_validator(mode="before")` that splits strings (use `NoDecode` annotation so pydantic-settings doesn't JSON-parse them).
- [ ] **Step 5: Run** – expect PASS.
- [ ] **Step 6: Commit** `feat: project scaffold and settings`

---

### Task 2: Image reference parsing

**Files:** Create `app/imageref.py`, `tests/test_imageref.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class ImageRef: registry: str; repository: str; tag: str | None; digest: str | None`, property `api_host -> str` (`registry-1.docker.io` for `docker.io`, else registry), property `is_docker_hub -> bool`, method `with_tag(tag) -> str` (the reference as the user would write it, preserving whether registry/`library/` was written – store original prefix in a field `original_name: str`). `parse_image_ref(ref: str) -> ImageRef`.

- [ ] **Step 1: Write failing tests** (parametrised):

| input | registry | repository | tag | digest |
|---|---|---|---|---|
| `nginx` | `docker.io` | `library/nginx` | `latest` | None |
| `nginx:1.25-alpine` | `docker.io` | `library/nginx` | `1.25-alpine` | None |
| `louislam/uptime-kuma:1` | `docker.io` | `louislam/uptime-kuma` | `1` | None |
| `ghcr.io/owner/app:v2.1.0` | `ghcr.io` | `owner/app` | `v2.1.0` | None |
| `localhost:5000/app:dev` | `localhost:5000` | `app` | `dev` | None |
| `lscr.io/linuxserver/plex` | `lscr.io` | `linuxserver/plex` | `latest` | None |
| `nginx@sha256:abc…` | `docker.io` | `library/nginx` | None | `sha256:abc…` |
| `docker.io/library/redis:7` | `docker.io` | `library/redis` | `7` | None |

Plus: `parse_image_ref("nginx:1.25").with_tag("1.26") == "nginx:1.26"`; `parse_image_ref("ghcr.io/o/a:1").with_tag("2") == "ghcr.io/o/a:2"`.

- [ ] **Step 2: Run** – FAIL.
- [ ] **Step 3: Implement.** First path component is a registry only if it contains `.` or `:` or equals `localhost`.
- [ ] **Step 4: Run** – PASS.
- [ ] **Step 5: Commit** `feat: image reference parsing`

---

### Task 3: Version tag comparison

**Files:** Create `app/versions.py`, `tests/test_versions.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class ParsedTag: prefix: str; parts: tuple[int, ...]; suffix: str`; `parse_version_tag(tag: str) -> ParsedTag | None`; `@dataclass(frozen=True) class VersionUpdates: minor: str | None; major: str | None`; `find_version_updates(current: str, tags: Iterable[str]) -> VersionUpdates`.

Rules (from spec): regex `^(v?)(\d+(?:\.\d+)*)(.*)$`; non-version tags (`latest`, `stable`, `lts`, `mainline`) → `None`. A candidate must have the same prefix, same suffix, same number of numeric parts, no pre-release marker anywhere in the tag, and its first part's digit count must be equal to or one more than the current's (filters date tags like `20240101`). `minor` = highest candidate with same major, greater than current. `major` = highest candidate with greater major. A single-part tag (`16`) can only have a `major`.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("current,tags,minor,major", [
    ("16.2", ["16.3", "16.4", "17.0", "17.0-alpine", "16.5-rc1", "16", "16.10"], "16.10", "17.0"),
    ("1.25.4-alpine", ["1.25.5-alpine", "1.26.0-alpine", "1.25.5", "2.0.0-alpine", "1.27.0-alpine-beta"], "1.26.0-alpine", "2.0.0-alpine"),
    ("v2.11.3", ["v2.11.4", "2.12.0"], "v2.11.4", None),
    ("16", ["17", "16", "15", "20240101"], None, "17"),
    ("8.0-bookworm", ["8.1-bookworm", "8.1-bullseye", "9.0-BETA-bookworm"], "8.1-bookworm", None),
    ("3.2.1", ["3.2.0", "3.2.1"], None, None),
    ("1.0.0", ["1.0.1-RC1", "1.0.1-Preview", "2.0.0-nightly"], None, None),
])
def test_find_version_updates(current, tags, minor, major):
    assert find_version_updates(current, tags) == VersionUpdates(minor, major)

@pytest.mark.parametrize("tag", ["latest", "stable", "lts", "mainline", "alpine"])
def test_non_version_tags(tag):
    assert parse_version_tag(tag) is None
```

- [ ] **Step 2: Run** – FAIL. **Step 3: Implement.** **Step 4: Run** – PASS.
- [ ] **Step 5: Commit** `feat: version tag comparison`

---

### Task 4: Registry client

**Files:** Create `app/registry.py`, `tests/test_registry.py`

**Interfaces:**
- Consumes: `ImageRef` (Task 2).
- Produces: `class RegistryError(Exception)`, `class RateLimitError(RegistryError)` (message `"Docker Hub rate limit reached – try again later"` for Hub, `"<registry> rate limit reached – try again later"` otherwise). `class RegistryClient(http: httpx.AsyncClient, docker_config_path: str | None = None, tag_cache_ttl: float = 86400, max_tag_pages: int = 20)` with `async head_digest(ref: ImageRef) -> str` and `async list_tags(ref: ImageRef) -> list[str]`.

Behaviour: `HEAD https://<api_host>/v2/<repo>/manifests/<tag>` with `Accept` listing OCI index, Docker manifest list v2, OCI manifest, Docker manifest v2; return `Docker-Content-Digest`. On 401 parse `WWW-Authenticate: Bearer realm=…,service=…,scope=…`, fetch a token (basic auth from docker `config.json` `auths` if present for that registry), cache it per (registry, repo), retry once. 429 → `RateLimitError`; other non-2xx / network error → `RegistryError` with status/reason. Tags: Docker Hub uses `https://hub.docker.com/v2/repositories/<repo>/tags?page_size=100&ordering=last_updated` following `next`; others use `/v2/<repo>/tags/list?n=1000` following `Link: <…>; rel="next"`. Stop after `max_tag_pages`. Cache tag lists per repo for `tag_cache_ttl` seconds (injectable clock `time.monotonic`).

- [ ] **Step 1: Write failing tests (respx)**

```python
async def test_head_digest_with_bearer_auth(respx_mock): ...
    # first HEAD → 401 with WWW-Authenticate; token endpoint → {"token": "t"}; HEAD with "Bearer t" → 200 + Docker-Content-Digest: sha256:aaa
    assert await client.head_digest(parse_image_ref("nginx:1.25")) == "sha256:aaa"
    assert respx_mock.calls  # assert method of manifest calls is HEAD, never GET

async def test_rate_limit_raises(respx_mock):  # 429 → RateLimitError, str contains "Docker Hub rate limit reached"
async def test_ghcr_tags_follow_link_header(respx_mock):  # 2 pages → combined list
async def test_hub_tags_use_hub_api_and_page_cap(respx_mock):  # max_tag_pages=2 with 3 pages available → only 2 requested
async def test_tag_list_cached(respx_mock):  # two list_tags calls → one HTTP call; after ttl → two
async def test_timeout_raises_registry_error(respx_mock):  # httpx.ConnectTimeout → RegistryError
async def test_docker_config_credentials_used(respx_mock, tmp_path):  # config.json auths base64 → token request has Authorization: Basic
```

- [ ] **Step 2: Run** – FAIL. **Step 3: Implement.** **Step 4: Run** – PASS.
- [ ] **Step 5: Commit** `feat: registry client`

---

### Task 5: SQLite store

**Files:** Create `app/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `class Store(path: str)` – creates tables on init (`check_results(id, checked_at, payload JSON)`, `jobs(id, kind, target, user, status, started_at, finished_at, log TEXT)`). Methods: `save_check(payload: dict, checked_at: datetime) -> None`, `latest_check() -> tuple[datetime, dict] | None`, `create_job(kind: str, target: str, user: str) -> int`, `append_log(job_id: int, text: str) -> None`, `finish_job(job_id: int, status: Literal["succeeded", "failed"]) -> None`, `get_job(job_id: int) -> Job | None`, `list_jobs(limit: int = 50) -> list[Job]`, `mark_interrupted_jobs() -> None` (any `running` job → `failed` with log line "Interrupted – the dashboard restarted"). `@dataclass class Job: id, kind, target, user, status, started_at, finished_at, log`.
- Use one connection with `check_same_thread=False` and a `threading.Lock`; WAL mode.

- [ ] **Step 1: Failing tests:** round-trip check payload; latest returns newest; job lifecycle (create → status `running`, append twice → log concatenated, finish → status + `finished_at` set); `list_jobs` newest first; `mark_interrupted_jobs` flips running jobs only.
- [ ] **Steps 2–4:** run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `feat: sqlite store`

---

### Task 6: Docker inventory

**Files:** Create `app/inventory.py`, `tests/test_inventory.py`

**Interfaces:**
- Produces: `@dataclass class ContainerInfo: id: str; name: str; image_ref: str; image_id: str; running_repo_digests: list[str]; local_tag_image_id: str | None; project: str | None; working_dir: str | None; config_files: list[str]; service: str | None; state: str; is_self: bool`. `list_containers(client: docker.DockerClient, self_id: str | None) -> list[ContainerInfo]`. `detect_self_id() -> str | None` (match `HOSTNAME` env against container ID prefix).
- `image_ref` = `container.attrs["Config"]["Image"]`; `image_id` = `container.attrs["Image"]`; `running_repo_digests` from `client.images.get(image_id).attrs["RepoDigests"]`, `[]` if the image is gone (`docker.errors.ImageNotFound`); `local_tag_image_id` = `client.images.get(image_ref).id` or None if not found. Compose labels: `com.docker.compose.project`, `…project.working_dir`, `…project.config_files` (comma-separated), `…service`. `all=True`.

- [ ] **Step 1: Failing tests** with a fake client (simple stub classes, not MagicMock soup): compose container parsed with two config files; non-compose container has `project=None`; exited container included with `state="exited"`; image removed locally → `running_repo_digests == []` and no exception; self detected via `HOSTNAME`.
- [ ] **Steps 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat: docker inventory`

---

### Task 7: Checker

**Files:** Create `app/checker.py`, `tests/test_checker.py`

**Interfaces:**
- Consumes: `ContainerInfo`, `RegistryClient`, `find_version_updates`, `parse_image_ref`, `MailcowStatus` (Task 11 – define the dataclass here in `checker.py` as `MailcowStatus(update_available: bool | None, current_version: str | None, latest_release: str | None, error: str | None)` and have Task 11 import it).
- Produces: `class Status(StrEnum): UP_TO_DATE, NEWER_BUILD, PULLED_NOT_RECREATED, NOT_CHECKABLE, ERROR`. `@dataclass class ContainerStatus: container: ContainerInfo; status: Status; remote_digest: str | None; minor_update: str | None; major_update: str | None; message: str | None; updatable: bool; not_updatable_reason: str | None`. `@dataclass class CheckResult: checked_at: datetime; containers: list[ContainerStatus]; mailcow: MailcowStatus | None` with `to_dict()` / `CheckResult.from_dict()` for the store, and `summary() -> dict[str, int]` (keys `up_to_date`, `updates`, `major`, `errors`). `async check_all(containers: list[ContainerInfo], registry: RegistryClient, settings: Settings, mailcow_check: Callable[[], Awaitable[MailcowStatus]] | None) -> CheckResult`.

Rules:
- Containers whose `project == "mailcowdockerized"` or `working_dir == settings.mailcow_dir` are excluded from `containers` and covered by `mailcow` (call `mailcow_check` only if any exist and the dir exists).
- Each distinct `image_ref` hits the registry once (digest + tags concurrently, `asyncio.gather` across images, return_exceptions so one failure doesn't stop the others).
- Digest ref or no `running_repo_digests` → `NOT_CHECKABLE` ("Image built locally" / "Pinned by digest").
- Remote digest in running digests → `UP_TO_DATE`; else if `local_tag_image_id` ≠ `image_id` and the local tag's digests contain the remote digest → `PULLED_NOT_RECREATED`; else `NEWER_BUILD`.
- Version updates only when the tag parses as a version; tag-list failure leaves the digest result intact and adds a message.
- `updatable` false with reason when: no compose project ("Not started by compose"), `working_dir` not present on disk ("Project files not mounted"), or `state` is not running/exited ("Container is <state>").
- Registry errors → `ERROR` with the error message (rate-limit message verbatim).

- [ ] **Step 1: Failing tests** (fake registry object with dict-driven responses): up to date; newer build; pulled-not-recreated; locally built → not checkable; **one image's registry raises `RegistryError`, the other is still `UP_TO_DATE`** (Review Focus 1); `RateLimitError` message surfaces; two containers sharing an image → one registry call; minor + major populated for `postgres:16.2`; mailcow containers collapsed and `mailcow_check` called once; `working_dir` missing on disk → `updatable False, "Project files not mounted"`; `to_dict`/`from_dict` round-trip.
- [ ] **Steps 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat: update checker`

---

### Task 8: Compose file tag editing

**Files:** Create `app/compose_edit.py`, `tests/test_compose_edit.py`

**Interfaces:**
- Produces: `class EditRefused(Exception)` (message shown to the user, ends with "– update by hand"). `@dataclass class TagChange: path: str; line_no: int; old_line: str; new_line: str; diff: str`. `plan_tag_change(config_files: list[str], service: str, new_image: str) -> TagChange`. `apply_tag_change(change: TagChange, backup_dir: str, project: str, now: datetime) -> str` (returns backup path; writes backup first; verifies the line still equals `old_line` else `EditRefused("Compose file changed since preview")`). `restore_backup(change: TagChange, backup_path: str) -> None`.

Algorithm (line scan, no YAML library): search config files last-to-first (later overrides win). Find top-level `services:`; within it the line `^(\s+)<service>:\s*(#.*)?$` setting the service indent; within that block (lines more indented than the service key) the first `image:` key at the service's child indent. Replace only the value token, keeping quote style, trailing comment and the line's original ending (`\r\n` or `\n`). Refuse if the value contains `${` ("Version set by variable"), or if no `image:` found in any listed file ("Image line not found – it may come from an include or extends file"). Read/write bytes to preserve encoding and endings. `diff` is a unified diff (`difflib`) of just that file.

- [ ] **Step 1: Failing tests**

```python
def test_replaces_only_target_service(tmp_path):  # two services both nginx:1.25 → only "web" line changes; rest byte-identical
def test_preserves_quotes_and_comment(tmp_path):  # image: "postgres:16.2"  # pinned → image: "postgres:16.4"  # pinned
def test_preserves_crlf(tmp_path):                # file with \r\n → output still \r\n everywhere
def test_refuses_variable(tmp_path):              # image: postgres:${PG} → EditRefused, "Version set by variable"
def test_override_file_wins(tmp_path):            # base + override both set image → override edited
def test_missing_image_refused(tmp_path):         # service has build: only → EditRefused, "Image line not found"
def test_apply_writes_backup_then_file(tmp_path)  # backup at backup_dir/<project>/docker-compose.yml.20261017-080000 equals original
def test_apply_refuses_if_changed(tmp_path)       # file edited after plan → EditRefused
def test_restore(tmp_path)                        # restore_backup → original bytes
```

- [ ] **Steps 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat: compose tag editing`

---

### Task 9: Job runner

**Files:** Create `app/jobs.py`, `tests/test_jobs.py`

**Interfaces:**
- Consumes: `Store` (Task 5).
- Produces: `class BusyError(Exception)` (message per Global Constraints). `class JobContext` with `job_id: int`, `log(text: str) -> None`, `async run(cmd: list[str], cwd: str | None = None, timeout: float = 1800, stdin_devnull: bool = True) -> int` (streams merged stdout/stderr line by line into the log; returns exit code; on timeout kills the process, logs "Timed out after N s" and returns -1). `class JobRunner(store: Store)` with `submit(kind: str, target: str, user: str, work: Callable[[JobContext], Awaitable[None]]) -> int` (raises `BusyError` if a job is running; runs `work` as an `asyncio` task detached from the request; marks `succeeded` if it returns, `failed` if it raises, logging the exception message), `async subscribe(job_id: int) -> AsyncIterator[str]` (yields the log so far, then new lines until the job finishes, then a final `"__END__:<status>"`), `busy -> bool`.

- [ ] **Step 1: Failing tests:** command output appears in store log; non-zero exit + `raise` → `failed`; second submit while running → `BusyError`; timeout → -1 and log line; **subscriber that connects mid-job receives earlier lines then later ones; a subscriber that disconnects doesn't stop the job** (Review Focus 5).
- [ ] **Steps 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat: job runner`

---

### Task 10: Compose update actions

**Files:** Create `app/updaters.py`, `tests/test_updaters.py`

**Interfaces:**
- Consumes: `ContainerInfo`, `JobContext`, `TagChange`, `apply_tag_change`, `restore_backup`.
- Produces:
  - `compose_cmd(info: ContainerInfo, *args: str) -> list[str]` → `["docker", "compose", "-p", project, "-f", f1, "-f", f2, ..., *args]`, cwd = `working_dir`.
  - `async update_build(ctx, info, whole_project: bool = False) -> None` – `pull [service]` then `up -d [service]`; raises `UpdateFailed` on non-zero.
  - `async update_version(ctx, info, change: TagChange, backup_dir: str, wait: Callable[[ContainerInfo, float], Awaitable[bool]], health_timeout: float = 120) -> None` – apply change (log diff + backup path), pull; pull fails → restore, raise; `up -d`; then `wait`; if `up -d` fails or wait returns False → restore, `up -d` again, log "Rolled back to <old image>", raise `UpdateFailed`.
  - `async wait_healthy(client, info, timeout) -> bool` – poll the service's container until running (and `healthy` if it has a healthcheck).
  - `start_self_update(client, info) -> None` – `client.containers.run(<own image>, command=[...compose pull+up -d for own service...], remove=True, detach=True, volumes={socket, compose_root, working_dir same path})`.
  - `class UpdateFailed(Exception)`. If output contained `toomanyrequests`, the message is "Docker Hub rate limit reached – try again later" (Review Focus 2).

- [ ] **Step 1: Failing tests** with a fake `JobContext` recording commands and scripted exit codes/output: build update runs pull then up with correct `-p`/`-f`/service; whole-project omits service; pull exit 1 with `toomanyrequests` → `UpdateFailed` with rate-limit message; version update success leaves file edited; pull fails → file restored, no `up`; `wait` False → file restored, `up -d` called twice, "Rolled back" in log.
- [ ] **Steps 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat: compose update actions`

---

### Task 11: Mailcow

**Files:** Create `app/mailcow.py`, `tests/test_mailcow.py`

**Interfaces:**
- Consumes: `MailcowStatus` (Task 7), `JobContext`.
- Produces: `host_cmd(mailcow_dir: str, script: str) -> list[str]` = `["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-C", "--", "env", "-i", "bash", "-lc", f"cd {shlex.quote(mailcow_dir)} && {script}"]`. `async check_mailcow(mailcow_dir: str, run: Callable[[list[str]], Awaitable[tuple[int, str]]], http: httpx.AsyncClient) -> MailcowStatus` – `./update.sh --check`: 0 → True, 3 → False, else error with last output lines; current version from `git describe --tags --always` via `host_cmd`; latest from `https://api.github.com/repos/mailcow/mailcow-dockerized/releases/latest` `tag_name` (failure → None, not an error). `async update_mailcow(ctx: JobContext, mailcow_dir: str) -> None` – run `./update.sh --force` via `ctx.run(host_cmd(...), timeout=3600)`; exit 2 → log "update.sh updated itself – running it again" and run once more; final non-zero → raise `UpdateFailed` ("Mailcow update stopped – see log", plus hint about local changes if output contains `conflict` or `local changes`).

- [ ] **Step 1: Failing tests:** `host_cmd` exact list and quoting of a path with a space; check exit 0/3/1 mapping; GitHub failure → `latest_release None`, no error; update rc sequence `[2, 0]` → two runs, success; `[2, 2]` → exactly two runs then failure; `[1]` with "conflict" in output → failure message mentions local changes.
- [ ] **Steps 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat: mailcow check and update`

---

### Task 12: Authentication and CSRF

**Files:** Create `app/auth.py`, `tests/test_auth.py`

**Interfaces:**
- Consumes: `Settings`.
- Produces: `setup_auth(app: FastAPI, settings: Settings) -> None` – adds Starlette `SessionMiddleware` (`secret_key=session_secret`, `max_age=8*3600`, `https_only=True`, `same_site="lax"`), registers Authlib OAuth client `entra` (`server_metadata_url=https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration`, scope `openid profile email`, `code_challenge_method="S256"`), routes `GET /auth/login`, `GET /auth/callback`, `GET /auth/logout`, and an auth-guard middleware. Redirect URI = `PUBLIC_URL + "/auth/callback"`. `user_from_claims(claims: dict, settings) -> User | None` (checks `tid == tenant`, `ALLOWED_EMAILS` against `preferred_username`/`email` case-insensitively, `ALLOWED_GROUP_ID` in `groups`). `@dataclass class User: name: str; email: str`. Dependency `current_user(request) -> User`. `csrf_token(request) -> str` (stored in session) and dependency `verify_csrf(request)` checking form field `csrf_token` or header `X-CSRF-Token` on POST → 403.
- Guard: paths starting `/auth/` or `/static/` pass; otherwise no session user → 302 to `/auth/login` for `GET` with `Accept: text/html`, 401 otherwise.

- [ ] **Step 1: Failing tests:**

```python
def test_every_route_requires_auth(client_app):
    # iterate app.routes; for every non-/auth, non-/static path (fill path params with "1"), GET and POST without a session
    # → status in {302, 401, 405}, and never 200
def test_user_from_claims_wrong_tenant_rejected(settings)
def test_allowed_emails_enforced(settings)
def test_allowed_group_enforced(settings)
def test_post_without_csrf_is_403(logged_in_client)
def test_sse_without_session_is_401(client_app)  # GET /jobs/1/stream with Accept: text/event-stream
```

(Logged-in test client: a test-only helper that sets the session via a signed cookie made with the same secret.)
- [ ] **Steps 2–4:** FAIL, implement, PASS. (The route-iteration test runs against the full app from Task 13 too – keep it in this file and it will cover new routes automatically.)
- [ ] **Step 5: Commit** `feat: entra sign-in and csrf`

---

### Task 13: Web UI and routes

**Files:** Create `app/main.py`, `app/templates/base.html`, `dashboard.html`, `job.html`, `history.html`, `app/static/app.css`, `app/static/app.js`, `tests/test_routes.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `create_app(settings: Settings, *, docker_client=None, http=None, store=None, runner=None, start_scheduler: bool = True) -> FastAPI` (dependencies injectable for tests; stored on `app.state`). `async run_check(app) -> CheckResult` (inventory → `check_all` → `store.save_check`) used by routes and scheduler.

Routes (all POSTs require CSRF; all return redirects or HTML fragments):
- `GET /` – dashboard from `store.latest_check()` (if none, run a check first). Header: server name, last checked (UK date format `17 Oct 2026, 08:00`), "Check now", user name, sign out. Summary counts. Table grouped by project ("Other" last): container, service, image, short running digest (12 chars), status badge, newest versions, actions. "Show only updates" toggle (client-side, remembered in `localStorage` with try/catch). Mailcow card at top when present.
- `POST /check` → runs check, redirect `/`.
- `POST /containers/{id}/update` (form `mode=build|project`) → `runner.submit(...)`, redirect `/jobs/{job_id}`. `BusyError` → redirect `/` with flash message.
- `GET /containers/{id}/version-preview?tag=` → HTML fragment with the diff from `plan_tag_change` (or the `EditRefused` message) and a confirm form. Only allowed for the container's `minor_update` tag – any other tag → 400.
- `POST /containers/{id}/version` (form `tag`) → re-plans, submits `update_version` job.
- `POST /mailcow/update` → submits `update_mailcow` job.
- `POST /self/update` → `start_self_update`, page saying "Updating the dashboard – reload in a minute".
- `POST /digest/send` → sends digest now, flash result.
- `GET /jobs/{id}` – log page; `app.js` opens `EventSource('/jobs/{id}/stream')`, appends lines, shows success/failure on `__END__`, then a "Back to dashboard" link. After each job finishes, the runner triggers `run_check`.
- `GET /jobs/{id}/stream` – SSE via `StreamingResponse` from `runner.subscribe`.
- `GET /history` – `store.list_jobs()` table with links to logs.
- Major updates show a warning badge: "Major version – may need a data migration. Update by hand."
- Self row: shows status; its button posts to `/self/update`.
- Layout works at 375 px wide (table collapses to cards below 700 px).

- [ ] **Step 1: Failing tests** (logged-in client, fake docker/registry/runner): dashboard lists grouped containers and badges; "Not started by compose" rows have no update button; version-preview with non-offered tag → 400; preview shows diff lines; POST update while busy → redirect with "Another update is running"; history lists jobs; dashboard loads with a container whose image was removed (Review Focus 4).
- [ ] **Steps 2–4:** FAIL, implement, PASS. Then run the whole suite `pytest -q` – all PASS, including Task 12's route-iteration test.
- [ ] **Step 5: Commit** `feat: dashboard ui and routes`

---

### Task 14: Weekly digest

**Files:** Create `app/digest.py`, `app/templates/digest.html`, `app/templates/digest.txt`, `tests/test_digest.py`

**Interfaces:**
- Consumes: `CheckResult`, `Settings`.
- Produces: `render_digest(result: CheckResult, settings: Settings) -> tuple[str, str, str] | None` (subject, html, plain) – `None` when nothing to report and `digest_when_none` is False. Subject: `"<SERVER_NAME>: N updates available"` or `"<SERVER_NAME>: all up to date"`. Sections in order: Mailcow, Newer build (incl. pulled-not-recreated), Minor/patch, Major, Not checkable / errors. Footer: last checked time and link to `PUBLIC_URL`. `send_digest(settings: Settings, result: CheckResult, email_client_factory=EmailClient.from_connection_string) -> str` – returns "sent" / "skipped – nothing to report" / "skipped – email not configured"; uses `begin_send({"senderAddress": digest_from, "recipients": {"to": [{"address": a} for a in digest_to]}, "content": {"subject", "html", "plainText"}}).result()`.

- [ ] **Step 1: Failing tests:** subject wording for 0 and 3 updates; sections appear only when non-empty; errors listed; `digest_when_none=False` + no updates → `None`; send uses factory with connection string and correct recipients (fake client); not configured → "skipped – email not configured".
- [ ] **Steps 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat: weekly digest email`

---

### Task 15: Scheduler

**Files:** Create `app/scheduler.py`, `tests/test_scheduler.py`; Modify `app/main.py` (start/stop in lifespan, call `store.mark_interrupted_jobs()` on startup)

**Interfaces:**
- Produces: `build_scheduler(app: FastAPI, settings: Settings) -> AsyncIOScheduler` with job `check` (`IntervalTrigger(hours=check_interval_hours)`, `next_run_time=now` so it also runs at startup) and, when `digest_enabled`, job `digest` (`CronTrigger.from_crontab(digest_schedule, timezone=tz)`) which runs a fresh check then `send_digest`. Both jobs `max_instances=1`, `coalesce=True`.

- [ ] **Step 1: Failing tests:** jobs registered with expected triggers; digest job absent when not configured; next digest fire time for `0 8 * * MON` from Fri 17 Oct 2026 is Mon 19 Oct 2026 08:00 Europe/London.
- [ ] **Steps 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat: scheduled checks and digest`

---

### Task 16: Packaging, CI, README and examples

**Files:** Create `Dockerfile`, `.dockerignore`, `.github/workflows/build.yml`, `README.md`, `examples/caddy/compose.yml`, `examples/caddy/Caddyfile`, `examples/traefik/compose.yml`, `examples/mailcow/compose.yml`

- [ ] **Step 1: Dockerfile** – `python:3.12-slim`; install `docker-ce-cli` and `docker-compose-plugin` from Docker's apt repo, plus `util-linux` (nsenter); `pip install .`; `VOLUME /data`; `EXPOSE 8000`; `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]` (`app.main:app` built via `create_app(get_settings())`).
- [ ] **Step 2: Verify** `docker build -t vud:test .` succeeds and `docker run --rm vud:test docker compose version` prints a version.
- [ ] **Step 3: Workflow** – on push to `main` and tags `v*`: run `pytest`, then `docker/setup-qemu-action`, `docker/setup-buildx-action`, `docker/login-action` to `ghcr.io` with `GITHUB_TOKEN`, `docker/metadata-action` (tags `latest`, semver, sha), `docker/build-push-action` with `platforms: linux/amd64,linux/arm64`. `permissions: packages: write, contents: read`. Pin each action to a major version.
- [ ] **Step 4: Examples** – all mount `/var/run/docker.sock:/var/run/docker.sock`, `/docker:/docker`, a named volume at `/data`, `restart: unless-stopped`, and the env vars from the spec table. Caddy: `updates.example.com { reverse_proxy docker-manager:8000 }` on a shared network. Traefik: router/service labels with TLS cert resolver placeholder. Mailcow: adds `pid: host`, `privileged: true`, `/opt/mailcow-dockerized:/opt/mailcow-dockerized`.
- [ ] **Step 5: README** (UK English, en dashes): what it does; Entra app registration (single tenant, web redirect `https://<host>/auth/callback`, client secret, Enterprise Application → **Assignment required = Yes**, assign users – and the warning that without it anyone in the tenant can sign in; optional groups claim for `ALLOWED_GROUP_ID`); ACS Email setup (resource, verified domain/sender, connection string); per-server install with each example; security note (socket = root on host already; `pid: host`/`privileged` on the Mailcow server removes friction rather than adding a new class of risk; the Entra sign-in is the protection); Docker Hub anonymous limits and the rate-limit message; **hPanel check** – after the first minor/patch update, open the project in hPanel and confirm it shows the new tag; if hPanel reverts it, set the project aside and update in hPanel.
- [ ] **Step 6: Run** `pytest -q` – all PASS.
- [ ] **Step 7: Commit** `chore: docker image, ci, readme and examples`

---

### Task 17: First real-server check (with the owner)

Not code – a checklist to run with the owner on one non-Mailcow server, then the Mailcow server.

- [ ] Push to GitHub, confirm the GHCR image builds for both architectures and the package is public.
- [ ] Deploy with the Caddy or Traefik example; sign in with an assigned account; confirm an unassigned account is refused.
- [ ] Confirm every container under `/docker` appears with a sensible status and nothing shows "Project files not mounted".
- [ ] Run one newer-build update and one minor/patch update; confirm hPanel shows the new tag afterwards (spec assumption).
- [ ] On the Mailcow server: "Check" result matches running `./update.sh --check` by hand; run the update from the dashboard when an update is available.
- [ ] "Send digest now" arrives from the ACS sender.
