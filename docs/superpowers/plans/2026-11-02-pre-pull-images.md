# Download updates ahead and tidy unused images – implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After each check, download the images for updates on offer in the background, show which are ready, and add an Images page (plus a dashboard card and digest line) for seeing and deleting unused images.

**Architecture:** A new `app/prefetch.py` works out what to download from the check result and the full container list, and a `Prefetcher` runs those pulls one at a time as a background task outside the `JobRunner`, keeping per-image state in memory. A new `app/images.py` scans Docker for images no container uses, works out which are protected (downloaded for an update still on offer), records "unused since" in the store, and its summary is saved on the `CheckResult` so the dashboard card and digest read it without Docker calls. Deleting runs as a normal job so it appears in History.

**Tech Stack:** Python 3, FastAPI, Jinja2, docker SDK (`docker` package), SQLite via `app/store.py`, pytest. Run tests with `.venv/bin/pytest`.

**Spec:** `docs/superpowers/specs/2026-11-02-pre-pull-images-design.md` – read it before starting any task.

## Global Constraints

- UK English in all copy, code comments and docs. En dashes (–), never em dashes.
- Commits: plain English saying what changed. No `Co-Authored-By` trailers, no "Generated with Claude Code" lines, no version numbers. Applies to every subagent.
- Do **not** bump `__version__` in `app/__init__.py`. CHANGELOG entry goes under `[Unreleased]` only (use the `dowo:changelog` skill).
- Updates are never applied automatically; tidying is never automatic. Every delete is a click by an Operator or Admin.
- Background downloads never go through `JobRunner` and never make `JobRunner.submit` raise `BusyError`.
- Never delete an image a container uses (running or stopped) or a protected ("Waiting to be applied") image.
- Never pre-download: major versions, Mailcow, containers not started by compose (`cs.updatable` false), the dashboard's own container (`is_self`), skipped/snoozed/unwatched updates, version tags set by a variable.
- Copy fixed by the spec: badges **"<tag> ready"** and **"Newer build ready"**, text **"Downloaded <time> ago"**, **"<tag> – downloading…"**, **"Couldn't download ahead – will try again at the next check"**; card **"Unused images – N images, X GB could be freed."** with **Review**; digest line **"N unused images, X GB"**; menu item **Images** between Layout and Users; buttons **Delete** and **Tidy all unused**; section **Waiting to be applied**.
- Settings → Checking: **Download updates ahead** (bool, default `True`); **Unused images warning (GB)** (float, default `10`, `0` turns the card off). Both page-only (`UI_ONLY`).
- Full suite (`.venv/bin/pytest -q`, 1,442 tests at the start) passes at the end of every task.

## Review Focus

1. **Shared tag with a skipped or unwatched sibling** – two containers on `postgres:16`, one with the build update skipped: nothing is pulled for that tag. Test in Task 3.
2. **Image with several tags, or untagged** – deleting must remove each tag then the image, and an untagged image is shown as "untagged" with a 12-character ID. Tests in Tasks 2 and 6.
3. **Docker refuses a delete mid-tidy** (a container started using it, 409) – the job logs it as skipped and carries on with the rest; the job still succeeds. Test in Task 6.
4. **Check runs again while a download round is running** – the new round waits and replaces any queued round; never two pulls at once. Test in Task 4.
5. **Docker unreachable during the images scan** – the check still saves and `unused_images` is `None`; the card and digest line don't show. Test in Task 2.

---

### Task 1: Settings and store table

**Files:**
- Modify: `app/config.py` (Checking options block)
- Modify: `app/settings_service.py` (`UI_ONLY`, `FIELDS` after `layout_allowlist`)
- Modify: `app/store.py` (new `_migration_7`, add to `MIGRATIONS`, new `Store` methods)
- Test: `tests/test_settings_service.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `Settings.prefetch_updates: bool = True`, `Settings.unused_images_warn_gb: float = 10`.
- Produces: table `image_unused (image_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, before INTEGER NOT NULL DEFAULT 0)`.
- Produces: `Store.sync_unused_images(ids: set[str], now: datetime) -> dict[str, tuple[datetime, bool]]` – in one write transaction: inserts each id not yet recorded with `first_seen=now` and `before = 1` if the table had no rows **and** meta key `images_scanned` is unset; deletes rows not in `ids`; sets meta `images_scanned`; returns `{id: (first_seen, before)}` for every id in `ids`.
- Produces: `Store.forget_unused_images(ids: set[str]) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store.py
def test_sync_unused_images_marks_first_scan_as_before(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    t1 = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
    seen = store.sync_unused_images({"sha256:a"}, t1)
    assert seen == {"sha256:a": (t1, True)}
    t2 = t1 + timedelta(hours=6)
    seen = store.sync_unused_images({"sha256:a", "sha256:b"}, t2)
    assert seen["sha256:a"] == (t1, True)
    assert seen["sha256:b"] == (t2, False)

def test_sync_unused_images_drops_ids_used_again(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    t = datetime(2026, 11, 2, tzinfo=timezone.utc)
    store.sync_unused_images({"sha256:a", "sha256:b"}, t)
    assert set(store.sync_unused_images({"sha256:b"}, t)) == {"sha256:b"}
    later = t + timedelta(days=1)
    assert store.sync_unused_images({"sha256:a"}, later)["sha256:a"] == (later, False)

def test_empty_first_scan_still_ends_the_before_window(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    t = datetime(2026, 11, 2, tzinfo=timezone.utc)
    store.sync_unused_images(set(), t)
    assert store.sync_unused_images({"sha256:a"}, t)["sha256:a"] == (t, False)

# tests/test_settings_service.py
def test_prefetch_settings_defaults_and_bounds(settings, tmp_path):
    # Build a SettingsService as the existing tests in this file do.
    ...
    assert eff.prefetch_updates is True
    assert eff.unused_images_warn_gb == 10
    # saving -1 for unused_images_warn_gb is a validation error; 0 is accepted
```

Follow the existing pattern in `tests/test_settings_service.py` for building the service and saving the "checking" section; assert both fields are in `UI_ONLY`.

- [ ] **Step 2: Run them – expect FAIL** (`AttributeError: 'Store' object has no attribute 'sync_unused_images'`, missing settings)

Run: `.venv/bin/pytest tests/test_store.py tests/test_settings_service.py -q`

- [ ] **Step 3: Implement**

`Field("prefetch_updates", "Download updates ahead", "checking", "bool", "After each check, download the images for updates on offer, so updating is quicker. Nothing is applied until you click Update.")` and `Field("unused_images_warn_gb", "Unused images warning (GB)", "checking", "float", "Show a card on the dashboard when unused images take up more than this. 0 turns the card off.", 0, 10000)`. Add both names to `UI_ONLY`. Migration 7 creates the table only.

- [ ] **Step 4: Run the full suite – expect PASS**

Run: `.venv/bin/pytest -q`

- [ ] **Step 5: Commit** – "Add settings and storage for downloading updates ahead and unused images"

---

### Task 2: Scan for unused images

**Files:**
- Create: `app/images.py`
- Modify: `app/checker.py` (`CheckResult` gains `unused_images`; `to_dict`/`from_dict`)
- Modify: `app/main.py` (`run_check`)
- Test: `tests/test_images.py`, `tests/test_checker.py`

**Interfaces:**
- Consumes: `Store.sync_unused_images` (Task 1), `skips.visible_result`.
- Produces in `app/images.py`:

```python
@dataclass(frozen=True)
class ImageRow:
    id: str                 # full "sha256:…"
    tags: list[str]         # RepoTags without "<none>:<none>"; [] when untagged
    size: int               # bytes, image attrs["Size"]
    unused_since: datetime | None
    before: bool            # True: already unused at the first scan → show "before <date>"

    @property
    def name(self) -> str: ...   # ", ".join(tags) or f"untagged {short id}" (12 hex chars after "sha256:")

@dataclass(frozen=True)
class UnusedSummary:
    count: int
    size: int               # bytes, sum of ImageRow.size

@dataclass
class ImagesReport:
    unused: list[ImageRow]
    waiting: list[ImageRow]  # protected: downloaded for an update still on offer

    def summary(self) -> UnusedSummary: ...   # over `unused` only

def protected_ids(visible: CheckResult, image_id_for: Callable[[str], str | None]) -> set[str]
def scan(client, store: Store, visible: CheckResult | None, now: datetime) -> ImagesReport
def human_size(n: int) -> str    # "412 MB", "6.4 GB" – decimal units, one decimal place from GB up
```

- `protected_ids`: for each container in `visible` (skips already applied): if `status` is `NEWER_BUILD` or `PULLED_NOT_RECREATED`, protect `image_id_for(image_ref)` when it returns an id different from `image_id` (a live lookup, because a download that finished since the last check still shows `NEWER_BUILD` in the stored result); if `minor_update`, protect `image_id_for(parse_image_ref(image_ref).with_tag(minor_update))` when it returns an id. `image_id_for` returns `None` when the image isn't on the server.
- `scan`: `client.images.list()` (top-level, includes untagged); used ids = `{c.attrs["Image"] for c in client.containers.list(all=True)}`; unused = images not used and not protected; waiting = protected images present on the server and not used. Calls `store.sync_unused_images({ids of unused}, now)` for `unused_since`/`before`. Sorted by size, largest first.
- `CheckResult.unused_images: UnusedSummary | None = None`, serialised as `{"count", "size"}` or `None`; old stored results without the key load as `None`.
- `run_check`: after `check_all` and layout, inside the lock, `result.unused_images = (await asyncio.to_thread(images.scan, state.docker, state.store, skips_mod.visible_result(result, state.skips)[0], datetime.now(timezone.utc))).summary()`; any exception from the scan is logged with `log.warning(..., exc_info=True)` and leaves it `None` – the check still saves.

- [ ] **Step 1: Write the failing tests** in `tests/test_images.py`, with a small fake client (`images.list()` returning objects with `.id`, `.attrs = {"RepoTags": […], "Size": n}`; `containers.list(all=True)` returning objects with `.attrs = {"Image": id}`):

```python
def test_scan_lists_images_no_container_uses(tmp_path): ...
    # images a (used by a stopped container), b (unused, tags ["redis:7.0"]), c (untagged)
    # → unused ids == {b, c}; c.name == "untagged 0123456789ab"; waiting == []
def test_protected_build_download_is_waiting_not_unused(tmp_path): ...
    # also when the stored status is still NEWER_BUILD (downloaded since the check)
    # container status PULLED_NOT_RECREATED, image_id "sha256:old", local_tag_image_id "sha256:new"
    # → "sha256:new" in waiting, not in unused; "sha256:old" is used, in neither
def test_protected_version_download_is_waiting(tmp_path): ...
    # minor_update "16.4" on postgres:16.2, image_id_for("postgres:16.4") → "sha256:v164"
def test_skipped_update_is_not_protected(tmp_path): ...
    # pass a visible result where the skip cleared minor_update → image appears in unused
def test_summary_counts_unused_only(): ...
def test_human_size():
    assert human_size(412_000_000) == "412 MB"
    assert human_size(6_400_000_000) == "6.4 GB"
    assert human_size(0) == "0 B"
```

In `tests/test_checker.py`: round-trip `CheckResult` with `unused_images=UnusedSummary(9, 6_400_000_000)` through `to_dict`/`from_dict`, and `from_dict` of a dict without the key gives `None`. Review Focus 5 test (in `tests/test_routes.py` or wherever `run_check` is tested): a docker stub whose `images.list` raises `docker.errors.APIError` – `run_check` returns a saved result with `unused_images is None`.

- [ ] **Step 2: Run – expect FAIL** (`ModuleNotFoundError: app.images`)

Run: `.venv/bin/pytest tests/test_images.py tests/test_checker.py -q`

- [ ] **Step 3: Implement** as specified above. `StubDocker` in `tests/test_routes.py` may need `images.list`/`containers.list` returning `[]` – extend the stub rather than special-casing production code.

- [ ] **Step 4: Run the full suite – expect PASS**

- [ ] **Step 5: Commit** – "Find images no container uses after each check"

---

### Task 3: Work out what to download ahead

**Files:**
- Create: `app/prefetch.py` (eligibility part)
- Test: `tests/test_prefetch.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Target:
    key: str                 # state key: for build the container's image_ref; for version the new ref "postgres:16.4"
    kind: Literal["build", "version"]
    info: ContainerInfo      # one container to run the pull for
    ref: str                 # what to pull: image_ref (build) or repo:new tag (version)
    remote_digest: str | None  # build only: the digest the check found

def targets(visible: CheckResult, containers: list[ContainerInfo],
            editable: Callable[[ContainerStatus, str], bool]) -> list[Target]
```

Rules (`visible` has skips applied; `containers` is the full inventory list from `run_check`, Mailcow included):
- Skip containers with `is_self`, `not cs.updatable`, or no `service`.
- **Build**: `cs.status == NEWER_BUILD`. Eligible only if **every** container in `containers` with the same `image_ref` appears in `visible.containers`, has `status != NOT_WATCHED`, and has `status == NEWER_BUILD` (a sibling that's skipped shows `UP_TO_DATE` in `visible`, so it fails this). One target per `image_ref`, using the first eligible container.
- **Version**: `cs.minor_update` set, and `editable(cs, tag)` true. `editable` is passed in so tests don't touch files; production passes a function that calls `compose_edit.plan_tag_change(info.config_files, info.service, new_ref)` and returns `False` on `EditRefused`/`OSError`. One target per new ref.
- `major_update` is never a target. Mailcow containers are never in `visible.containers`, so they're never targets, and they block a build target that shares their tag.

- [ ] **Step 1: Write the failing tests**

```python
def test_newer_build_is_a_target(): ...
def test_shared_tag_with_skipped_sibling_is_not_a_target(): ...      # Review Focus 1
def test_shared_tag_with_unwatched_sibling_is_not_a_target(): ...
def test_shared_tag_with_mailcow_container_is_not_a_target(): ...
def test_shared_tag_all_watched_gives_one_target(): ...
def test_minor_version_is_a_target_with_new_ref():
    # postgres:16.2, minor_update "16.4" → Target(kind="version", ref="postgres:16.4", key="postgres:16.4")
def test_version_set_by_variable_is_not_a_target(): ...              # editable returns False
def test_major_only_dashboard_and_non_compose_are_never_targets(): ...
```

- [ ] **Step 2: Run – expect FAIL**

Run: `.venv/bin/pytest tests/test_prefetch.py -q`

- [ ] **Step 3: Implement `targets`.**

- [ ] **Step 4: Run the full suite – expect PASS**

- [ ] **Step 5: Commit** – "Decide which updates can be downloaded ahead"

---

### Task 4: Run the downloads in the background

**Files:**
- Modify: `app/prefetch.py` (add `State`, `Prefetcher`)
- Modify: `app/main.py` (create `app.state.prefetcher` in `create_app`; start a round at the end of `run_check`)
- Test: `tests/test_prefetch.py`, `tests/test_routes.py`

**Interfaces:**
- Consumes: `targets` (Task 3), `updaters.compose_cmd`, `mailcow.run_capture` (uses `subprocess_env()`, so credentials work as for Update pulls).
- Produces:

```python
@dataclass
class State:
    status: Literal["downloading", "ready", "failed"]
    at: datetime            # when the status was set

class Prefetcher:
    def __init__(self, run: Callable[[list[str], str | None], Awaitable[tuple[int, str]]],
                 present: Callable[[Target], bool], now: Callable[[], datetime]) -> None
    def request(self, targets: list[Target]) -> None   # sync; schedules a round on the running loop
    def state(self, key: str) -> State | None
    async def wait_idle(self) -> None                  # for tests
```

Behaviour:
- `request` replaces any queued round with these targets; if no round is running, starts one as a task (added to `app.state.background` by the caller via a `spawn` hook or by `request` taking it – implementer's choice, but the task must be tracked and its failure logged with `_log_task_failure`). Never two pulls at once (Review Focus 4).
- For each target in turn: if `present(target)` → `ready` (no pull). Else set `downloading`, run the pull, then `ready` on exit code 0 or `failed` otherwise (log a warning with the target ref; the output is not stored).
- Build pull: `compose_cmd(info, "pull", info.service)` with `cwd=info.working_dir`. Version pull: `["docker", "pull", "--platform", platform, ref]` where `platform` is `os/arch[/variant]` from the running image (`client.images.get(info.image_id).attrs`: `Os`, `Architecture`, `Variant`); omit `--platform` if those can't be read.
- `present` (production): build → `client.images.get(info.image_ref)` has a RepoDigest whose digest equals `remote_digest`; version → `client.images.get(ref)` succeeds. Any Docker error → `False`.
- States for keys not in the latest request are dropped at the start of a round.
- `run_check`: after the lock is released and `after_check` has run, if `state.settings.prefetch_updates`, call `state.prefetcher.request(prefetch.targets(visible, containers, editable))`. Wrap in try/except so a failure here never fails the check.
- Shutdown: the round task is in `app.state.background`, which is already cancelled on shutdown; a cancelled pull is simply abandoned.

- [ ] **Step 1: Write the failing tests** (fake `run` recording commands and returning `(0, "")` or `(1, "error")`; fake `present`; fixed `now`):

```python
async def test_round_pulls_each_target_once_in_order(): ...
async def test_present_image_is_ready_without_pulling(): ...
async def test_failed_pull_is_failed_and_round_continues(): ...
async def test_new_request_during_round_waits_and_replaces_queued(): ...   # Review Focus 4
    # run blocks on an Event for target A; request([B]) then request([C]); release →
    # commands == [pull A, pull C]; max concurrent == 1
async def test_version_pull_uses_running_image_platform(): ...
    # command == ["docker", "pull", "--platform", "linux/arm64/v8", "postgres:16.4"]
async def test_states_for_dropped_targets_are_forgotten(): ...
```

In `tests/test_routes.py`: with `prefetch_updates` off, `run_check` makes no `request`; with it on, a pending download never makes an Update POST return busy (`JobRunner.busy` stays `False` during a round).

- [ ] **Step 2: Run – expect FAIL**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the full suite – expect PASS**

- [ ] **Step 5: Commit** – "Download updates in the background after each check"

---

### Task 5: Show ready, downloading and failed on the dashboard

**Files:**
- Modify: `app/view.py` (new function; `_update_card` wording)
- Modify: `app/main.py` (`STATUS_LABELS`; pass `prefetch_note` to `dashboard.html`)
- Modify: `app/templates/dashboard.html` (status cell, around line 258)
- Test: `tests/test_view.py`, `tests/test_routes.py`

**Interfaces:**
- Consumes: `Prefetcher.state(key)` (Task 4).
- Produces:

```python
@dataclass(frozen=True)
class PrefetchNote:
    badge: str | None   # "16.4 ready" | "Newer build ready" | "16.4 – downloading…" | None
    text: str | None    # "Downloaded 2 h ago" | "Couldn't download ahead – will try again at the next check" | None
    ready: bool

def prefetch_note(cs: ContainerStatus, state_for: Callable[[str], State | None],
                  now: datetime) -> PrefetchNote | None
```

- `STATUS_LABELS[PULLED_NOT_RECREATED] = "Newer build ready"` (replaces "Pulled – not recreated"). `_update_card` for that status: badge `"Ready"`, text `"The newer build is downloaded – updating restarts it on the new image."`.
- `prefetch_note`: version (minor) → state key `parse_image_ref(image_ref).with_tag(minor_update)`; build (`NEWER_BUILD`/`PULLED_NOT_RECREATED`) → key `image_ref`. `PULLED_NOT_RECREATED` with no state is ready (badge `None` because the status label already says it; text `None`). Ready → text `f"Downloaded {ago} ago"` using the existing relative-time helper if there is one in `view.py`/templates, else `"just now"`/`"N min"`/`"N h"`/`"N days"`. Returns `None` when there's no update or no state.
- Template: under the status badge, show `note.badge` as a green `status_badge("current", …)` for ready (grey `"muted"` while downloading) and `note.text` in `<div class="meta">`. The Update buttons are unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_view.py` for each case: version ready ("16.4 ready", "Downloaded 2 h ago"), version downloading ("16.4 – downloading…"), build failed (text is the fixed copy, badge `None`), `PULLED_NOT_RECREATED` label "Newer build ready", no update → `None`. One route test: dashboard HTML contains "16.4 ready" when the prefetcher state says ready.

- [ ] **Step 2: Run – expect FAIL**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the full suite – expect PASS**

- [ ] **Step 5: Commit** – "Show on each container whether its update is downloaded"

---

### Task 6: Images page, Delete and Tidy all unused

**Files:**
- Create: `app/templates/images.html`
- Modify: `app/images.py` (add `delete_images`)
- Modify: `app/main.py` (routes; `JOB_KIND_LABELS["tidy"] = "Tidy images"`)
- Modify: `app/templates/base.html` (nav link between Layout and Users)
- Test: `tests/test_images.py`, `tests/test_routes.py` (or new `tests/test_images_routes.py`)

**Interfaces:**
- Consumes: `images.scan`, `ImagesReport`, `human_size` (Task 2); `submit` in `main.py`; the existing confirm dialog in `dashboard.html` (reuse its markup/JS pattern or move it to a shared partial).
- Produces:

```python
async def delete_images(ctx: JobContext, client, store: Store, ids: list[str]) -> None
```

- For each id: `image = client.images.get(id)` (gone → log "Already deleted" and continue); remove each tag with `client.images.remove(tag)` then, if the image still exists, `client.images.remove(id)` (no `force`). On `docker.errors.APIError` (e.g. 409 in use) log `f"Skipped {name} – {reason}"` and continue (Review Focus 3). Logs `f"Deleted {name} ({human_size(size)})"` per image and a final `f"Freed {human_size(total)}"`. Calls `store.forget_unused_images` for deleted ids. Docker calls go through `asyncio.to_thread`. The job succeeds even if some were skipped.
- Routes:
  - `GET /images` (Viewer): `report = await asyncio.to_thread(images.scan, docker, store, visible_latest, now)`; renders `images.html` with `report`, total, `can_delete = user.role >= Role.OPERATOR`. Docker error → page shows an error notice, not a 500.
  - `POST /images/{image_id}/delete` (Operator, CSRF): re-scan; 404 unless `image_id` is in `report.unused` (so protected and in-use images can never be deleted this way); `submit("tidy", name, user, lambda ctx: delete_images(ctx, docker, store, [image_id]))`.
  - `POST /images/tidy` (Operator, CSRF): re-scan; `submit("tidy", f"{n} unused images", user, …)` with every id in `report.unused`; nothing unused → redirect to `/images` with a flash "Nothing to tidy".
  - After a tidy job finishes, update the stored latest check's `unused_images` with a fresh `scan(...).summary()` so the card is current without waiting for the next check (do this at the end of the job's work function, guarded so a failure only logs).
- Page: heading "Unused images · N · X GB" with **Tidy all unused** (Operators/Admins); table of name, size, unused since (`"before 2 Nov 2026"` when `before`, else a date), sortable by size and unused since (client-side, like other tables); **Delete** per row. A **Waiting to be applied** section listing protected images with no buttons. Empty state: "No unused images."

- [ ] **Step 1: Write the failing tests**

```python
async def test_delete_removes_each_tag_then_the_image(): ...         # Review Focus 2
async def test_delete_skips_an_image_docker_refuses_and_continues(): ...  # Review Focus 3
def test_images_page_lists_unused_and_waiting(client_for): ...
def test_viewer_sees_no_delete_buttons(client_for): ...
def test_delete_of_waiting_or_used_image_is_404(client_for): ...
def test_tidy_submits_one_job_with_every_unused_image(client_for): ...
def test_viewer_cannot_post_tidy(client_for): ...                   # 403 like other Operator routes
def test_nav_has_images_between_layout_and_users(client_for): ...
```

- [ ] **Step 2: Run – expect FAIL**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the full suite – expect PASS**

- [ ] **Step 5: Commit** – "Add an Images page to delete unused images"

---

### Task 7: Dashboard card and digest line

**Files:**
- Modify: `app/view.py` (`attention` gains the card), `app/main.py` (pass the threshold), `app/templates/dashboard.html` (card's **Review** link instead of update buttons)
- Modify: `app/digest.py` (`render_digest` context), `app/templates/digest.html`, `app/templates/digest.txt`
- Test: `tests/test_view.py`, `tests/test_digest.py`

**Interfaces:**
- Consumes: `CheckResult.unused_images`, `human_size` (Task 2), `Settings.unused_images_warn_gb` (Task 1).
- Produces: `view.attention(result, unused_warn_gb: float = 0) -> list[Attention]` – appends `Attention("images", "Unused images", "", "Unused images", f"{count} images, {human_size(size)} could be freed.", None)` when `unused_warn_gb > 0` and `result.unused_images` and `size > unused_warn_gb * 1_000_000_000`. Card shows a **Review** link to `/images`. It carries no filter state (same handling as the Mailcow card – check `filter_attrs` copes with `cs is None` for a non-Mailcow card; give it its own branch if not).
- Digest: context key `unused_images = f"{count} unused images, {human_size(size)}"` (singular "image" for 1) when `count > 0`, with a link to `public_url + "/images"` when `public_url` is set; rendered in both templates next to the `skipped` line. It does **not** change the "nothing to report" rule: `render_digest` still returns `None` in the same cases as before.

- [ ] **Step 1: Write the failing tests**

```python
def test_card_shows_over_threshold(): ...          # 6.4 GB with warn 5 → card text "9 images, 6.4 GB could be freed."
def test_no_card_under_threshold_or_when_off(): ... # warn 10 → none; warn 0 → none
def test_no_card_when_scan_failed(): ...            # unused_images None
def test_digest_has_unused_line(): ...              # "9 unused images, 6.4 GB" in html and plain
def test_unused_line_alone_does_not_send_digest(): ... # no updates, digest_when_none False → None
```

- [ ] **Step 2: Run – expect FAIL**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the full suite – expect PASS**

- [ ] **Step 5: Commit** – "Warn about unused images on the dashboard and in the digest"

---

### Task 8: README and CHANGELOG

**Files:**
- Modify: `README.md` – Newer build section (rename "Pulled – not recreated" to "Newer build ready"; download ahead; the `up -d` / Save and apply side effect from the spec); a new **Images** section (unused vs waiting, Delete, Tidy all unused, "before <date>", sizes can overlap so the space freed may be less than the total); Settings → Checking (both new settings); weekly digest line; Docker Hub limits (pre-downloads are real pulls; one overtaken before use costs a pull for nothing); roles table (Images page for Viewers, Delete/Tidy for Operators); table of contents. Keep "It never updates anything on its own" true and add "or deletes".
- Modify: `CHANGELOG.md` under `[Unreleased]` – use the `dowo:changelog` skill. Short entries in the owner's style: one for downloading ahead, one for the Images page, card and digest line.

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Run the full suite – expect PASS** (`tests/test_packaging.py` checks the CHANGELOG/version agreement; the version must be unchanged).
- [ ] **Step 3: Commit** – "Document downloading updates ahead and the Images page"
