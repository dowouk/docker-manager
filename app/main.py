"""FastAPI app: dashboard pages, update actions and live job logs."""
import asyncio
import dataclasses
import functools
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import docker
import docker.errors
import httpx
import jinja2
from markupsafe import Markup, escape
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.auth import RoleDenied, User, csrf_token, require_role, setup_auth, verify_csrf
from app.entra import build_oauth, check_settings, redirect_uri, setup_entra
from app.photos import Photos, setup_photos
from app.checker import CheckResult, ContainerStatus, Status, check_all
from app.compose_edit import EditRefused, plan_tag_change
from app.config import Settings, get_settings
from app.notify.channels import (
    DEFAULT_EVENTS, EVENTS, Channel, ChannelError, ChannelStore,
)
from app.notify.notifier import NO_SUCH_CHANNEL, UNKNOWN_PROVIDER, Notifier
from app.notify.providers import (
    LOGIN_REFUSED, NO_ANSWER, NOT_PRIVATE, PROVIDERS, REJECTED, SMTP_PRESETS, UNREACHABLE,
    shutdown_notify_executor,
)
from app.imageref import parse_image_ref
from app.inventory import container_key, detect_self_id, list_containers
from app.links import release_notes_url
from app.jobs import BUSY_MESSAGE, BusyError, JobContext, JobRunner, set_docker_config_dir
from app.layout import layout_problems
from app.mailcow import check_mailcow, run_capture, update_mailcow
from app.keys import KeyUnusable, load_secret_box
from app.registry import RegistryClient, RegistryError
from app.registry_logins import (
    logins_by_host,
    normalise_host,
    test_login,
    write_docker_config,
)
from app.roles import Role
from app.rules import RulesError, RulesStore, shutdown_tag_executor
from app import skips as skips_mod
from app.skips import SkipStore
from app.sessions import COOKIE, SessionStore
from app.setup import Setup, setup_routes
from app.admin_pages import setup_admin_pages
from app.signin import setup_signin
from app.users import UserStore
from app.settings_service import (
    FIELDS, KEY_UNUSABLE_MESSAGE, MAPPABLE_ROLES, choices_for, SECTIONS, SettingsError, SettingsService, key_path,
)
from app.store import Store
from app import view
from app.updaters import (
    UpdateFailed,
    follow_self_update,
    remove_finished_helpers,
    start_self_update,
    update_build,
    update_version,
    wait_healthy,
)

log = logging.getLogger(__name__)
_HERE = os.path.dirname(__file__)
_END = "__END__:"
CHANGE_KINDS = {"settings": "Settings", "signin": "Sign-in", "account": "Account",
                "users": "Users"}
STREAM_RECHECK = 5.0  # seconds between access re-checks on an idle job-log stream

# Flash messages: only these fixed strings are ever shown (no reflected text).
# Each is (text, level); "ok" shows as a status note, "error" as an alert.
FLASH = {
    "busy": (BUSY_MESSAGE, "error"),
    "check_failed": ("The check could not be completed – see the server logs", "error"),
    "edit_refused": ("The compose file can't be edited automatically – update by hand", "error"),
    "changed": ("The compose file changed since the preview – review the change again", "error"),
    "skipped": ("Update hidden – it's listed under Skipped below", "ok"),
    "skip_removed": ("Skip removed – the update shows again", "ok"),
    "rules_saved": ("Watch settings saved – they apply from the next check", "ok"),
}

# Same idea for the Settings page.
SETTINGS_FLASH = {
    "saved": ("Settings saved", "ok"),
    "channel_saved": ("Notification channel saved", "ok"),
    "channel_removed": ("Notification channel removed", "ok"),
    "channel_reset": ("The unreadable notification channels were removed – you can add channels again", "ok"),
    "channel_reset_refused": ("Nothing was removed – tick the box to confirm, and the channels must still be unreadable", "error"),
    "channel_unavailable": ("The notification channels can't be changed right now – see the server logs", "error"),
    "registry_saved": ("Registry login saved", "ok"),
    "registry_removed": ("Registry login removed", "ok"),
    "entra_ok": ("Microsoft sign-in settings look right", "ok"),
    "entra_failed": ("Couldn't reach Microsoft with these settings – check the tenant ID", "error"),
    "group_saved": ("Group role saved", "ok"),
    "group_removed": ("Group role removed", "ok"),
    "digest_sent": ("Digest sent", "ok"),
    "digest_nothing": ("Digest skipped – nothing to report", "ok"),
    "digest_unconfigured": ("Digest skipped – no channel gets the digest", "ok"),
    "digest_failed": ("The digest could not be sent – check the server logs", "error"),
}

# Every reason a send can fail with, by position: the test result travels in the redirect as
# a number, never as text, so the flash can only ever show one of these.
CHANNEL_REASONS = (LOGIN_REFUSED, UNREACHABLE, REJECTED, NO_ANSWER, NOT_PRIVATE,
                   UNKNOWN_PROVIDER, NO_SUCH_CHANNEL)
CHANNEL_ID = re.compile(r"^(?:[0-9a-f]{16}|env)$")
RECIPIENT_SPLIT = re.compile(r"[,;\s]+")


def known_reason(reason: str | None) -> str:
    return reason if reason in CHANNEL_REASONS else REJECTED


STATUS_LABELS = {
    Status.UP_TO_DATE: "Up to date",
    Status.NEWER_BUILD: "Newer build available",
    Status.PULLED_NOT_RECREATED: "Pulled – not recreated",
    Status.NOT_CHECKABLE: "Can't check",
    Status.ERROR: "Error",
    Status.NOT_WATCHED: "Not watched",
}
JOB_STATUS_LABELS = {"running": "Running", "succeeded": "Succeeded", "failed": "Failed"}
JOB_KIND_LABELS = {
    "build": "Image update",
    "project": "Project update",
    "version": "Version update",
    "mailcow": "Mailcow update",
    "self": "Dashboard self-update",
}
SELF_RESTART_WAIT = 600.0
SELF_UPDATE_GRACE = 30.0


def _short_digest(cs: ContainerStatus) -> str:
    digests = cs.container.running_repo_digests
    if not digests:
        return "–"
    return digests[0].rsplit("@", 1)[-1].removeprefix("sha256:")[:12]


async def run_check(app: FastAPI) -> CheckResult:
    """Inventory → registry check → store. Concurrent calls are serialised."""
    state = app.state
    try:
        async with state.check_lock:
            await asyncio.to_thread(remove_finished_helpers, state.docker)
            containers = await asyncio.to_thread(list_containers, state.docker, detect_self_id())
            rules_for = state.rules.snapshot()
            result = await check_all(
                containers,
                state.registry,
                state.settings,
                mailcow_check=functools.partial(
                    check_mailcow, state.settings.mailcow_dir, run_capture, state.http
                ),
                rules_for=rules_for,
            )
            result.layout = layout_problems(
                containers, state.settings, rules_for,
                state.settings.data_dir, state.settings.mailcow_dir,
            ) if state.settings.layout_check else None
            state.store.save_check(result.to_dict(), result.checked_at)
    except Exception:
        state.notifier.after_check(None, True)  # never raises; may alert about repeat failures
        raise
    state.notifier.after_check(result, False)
    return result


def _latest(app: FastAPI) -> CheckResult | None:
    latest = app.state.store.latest_check()
    if not latest:
        return None
    try:
        return CheckResult.from_dict(latest[1])
    except (KeyError, TypeError, ValueError, AttributeError):
        # Saved by an older version in a different shape: treat as no check yet.
        log.warning("Ignoring a stored check that can't be read", exc_info=True)
        return None


def _offers_project(result: CheckResult | None, cs: ContainerStatus) -> bool:
    """"Update whole project" only when the image is shared within the project and
    the project doesn't contain this dashboard (it can't recreate itself mid-job)."""
    project = cs.container.project
    if not result or not project:
        return False
    members = [o.container for o in result.containers if o.container.project == project]
    if any(m.is_self for m in members):
        return False
    return sum(1 for m in members if m.image_ref == cs.container.image_ref) >= 2


def _find(app: FastAPI, container_id: str) -> ContainerStatus:
    result = _latest(app)
    for cs in result.containers if result else []:
        if cs.container.id == container_id:
            return cs
    raise HTTPException(status_code=404, detail="Unknown container")


def _find_key(app: FastAPI, key: str) -> ContainerStatus:
    result = _latest(app)
    for cs in result.containers if result else []:
        if container_key(cs.container) == key:
            return cs
    raise HTTPException(status_code=404, detail="Unknown container")


def _rule_fields(rules, form: Mapping[str, str] | None, errors: dict) -> list[dict]:
    """What the watch-settings form shows: the submitted text when re-showing errors,
    else the effective value (a label's value for a field a label sets)."""
    def text(field: str):
        value = getattr(rules, field)
        return "; ".join(value) if isinstance(value, tuple) else (value or "")

    specs = [
        ("watch", "Watch this container", "bool", "Switch off to skip registry checks for it."),
        ("include_tags", "Only consider these tags", "text",
         "Patterns separated by semicolons, matched against the whole tag. Empty means all tags."),
        ("exclude_tags", "Ignore these tags", "text",
         "Patterns separated by semicolons, matched against the whole tag."),
        ("notify", "Include in the digest and Updates found", "bool",
         "Switch off to see its updates here without being told about them."),
        ("release_notes", "Release notes link", "text", "A link starting with https:// or http://."),
        ("layout_ignore", "Leave out of the layout check", "bool", "Skip the file-layout checks for it."),
    ]
    fields = []
    for name, label, kind, help_ in specs:
        locked = name in rules.from_label
        value = text(name) if form is None or locked or name not in form else form[name]
        if kind == "bool":
            value = ("yes" if getattr(rules, name) else "no") if form is None or locked or name not in form \
                else str(form[name])
        fields.append({"name": name, "label": label, "kind": kind, "help": help_,
                       "locked": locked, "value": value, "error": errors.get(name)})
    return fields


def _require_updatable(cs: ContainerStatus) -> None:
    if not cs.updatable or cs.container.is_self:
        raise HTTPException(status_code=400, detail="This container can't be updated here")


def _require_offered_tag(cs: ContainerStatus, tag: str) -> None:
    if not cs.container.service:
        raise HTTPException(status_code=400, detail="The compose service is unknown")
    if not cs.minor_update or tag != cs.minor_update:
        raise HTTPException(status_code=400, detail="That version is not offered")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _log_task_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Background check after a job failed", exc_info=exc)


def apply_registry_logins(app: FastAPI) -> None:
    """Checks use saved logins from memory; `docker compose pull` gets them through
    the credential helper named in a generated config (paths only, no secrets)."""
    state = app.state
    logins = state.settings_service.registry_logins()
    state.registry.set_logins(logins_by_host(logins))
    db_path = getattr(state.store, "path", None)
    if db_path is None:  # a store without a known file: nothing for the helper to open
        set_docker_config_dir(None)
        return
    startup = state.startup_settings
    set_docker_config_dir(write_docker_config(
        os.path.join(startup.data_dir, "docker-config"),
        [item["host"] for item in logins if isinstance(item.get("host"), str)],
        db_path,
        key_path(startup),
        state.settings.registry_auth_file,
    ))


def apply_settings(app: FastAPI) -> None:
    """Make a saved settings change take effect in the running app."""
    app.state.settings = app.state.settings_service.effective()
    app.state.oauth = build_oauth(app.state.settings)
    apply_registry_logins(app)
    app.state.templates.env.globals["server_name"] = app.state.settings.server_name
    if app.state.scheduler is not None:
        from app.scheduler import reschedule  # deferred: scheduler imports this module

        reschedule(app)


def create_app(
    settings: Settings,
    *,
    docker_client=None,
    http: httpx.AsyncClient | None = None,
    store: Store | None = None,
    runner: JobRunner | None = None,
    start_scheduler: bool = True,
    providers=None,
    settings_service: SettingsService | None = None,
) -> FastAPI:
    owns_http = http is None
    if docker_client is None:
        docker_client = docker.from_env()
    if http is None:
        http = httpx.AsyncClient(timeout=20, follow_redirects=True)
    if store is None:
        store = Store(os.path.join(settings.data_dir, "dashboard.db"))
    if runner is None:
        runner = JobRunner(store)
    store.mark_interrupted_jobs()
    if settings_service is None:
        settings_service = SettingsService(
            settings, store, load_secret_box(key_path(settings))
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        app.state.photos.loop = asyncio.get_running_loop()  # for lookups from worker threads
        if start_scheduler:
            from app.scheduler import build_scheduler  # deferred: scheduler imports this module

            scheduler = build_scheduler(app, app.state.settings)
            scheduler.start()
            app.state.scheduler = scheduler
        try:
            yield
        finally:
            try:
                if scheduler is not None:
                    scheduler.shutdown(wait=False)
            finally:
                shutdown_notify_executor()
                shutdown_tag_executor()
                if owns_http:
                    await http.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.scheduler = None
    app.state.settings_service = settings_service
    app.state.settings = settings_service.effective()
    app.state.docker = docker_client
    app.state.http = http
    app.state.startup_settings = settings
    app.state.store = store
    app.state.registry = RegistryClient(http, app.state.settings.registry_auth_file)
    apply_registry_logins(app)
    app.state.runner = runner
    app.state.check_lock = asyncio.Lock()
    app.state.background = set()
    backup_dir = os.path.join(settings.data_dir, "backups")

    app.state.users = UserStore(store, settings_service._box)
    app.state.sessions = SessionStore(store)
    app.state.sessions.purge_expired()  # dead rows would otherwise pile up
    # Before anything is served: migrate old allow rules, or open first-start setup.
    app.state.setup = Setup(store, app.state.users, settings_service, settings.data_dir)
    app.state.setup.prepare(settings)
    app.state.rules = RulesStore(store)
    app.state.skips = SkipStore(store)
    app.state.channels = ChannelStore(store, settings_service._box, lambda: settings)
    app.state.channels.migrate_0_2_email()
    app.state.notifier = Notifier(
        store, app.state.channels, lambda: app.state.settings, http,
        providers if providers is not None else PROVIDERS,
        background=app.state.background, kind_labels=JOB_KIND_LABELS,
        skips_getter=lambda: app.state.skips, rules_snapshot=lambda: app.state.rules.snapshot())
    app.state.settings = settings_service.effective()  # the migrations may have changed it
    app.state.entra_setup_hook = app.state.setup.complete_with_entra
    app.state.setup.on_close.append(lambda: setattr(app.state, "entra_setup_hook", None))
    if not app.state.setup.is_open():
        app.state.entra_setup_hook = None
    setup_auth(app, settings, app.state.sessions, setup_open=app.state.setup.is_open)
    app.state.photos = Photos(lambda: app.state.users, lambda: app.state.http,
                              lambda: app.state.settings.gravatar)
    setup_entra(app)
    setup_photos(app)
    app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(_HERE, "templates")),
        autoescape=True,
    )

    def uk_date(dt: datetime | None) -> str:
        if dt is None:
            return "–"
        local = dt.astimezone(ZoneInfo(app.state.settings.tz))
        return f"{local.day} {local:%b %Y, %H:%M}"

    env.filters["uk_date"] = uk_date

    def breakable(text) -> Markup:
        # Long image names wrap after / and : rather than mid-word. Escaped first.
        return Markup(str(escape(text)).replace("/", "/<wbr>").replace(":", ":<wbr>"))

    env.filters["breakable"] = breakable
    env.globals.update(
        server_name=app.state.settings.server_name,
        app_version=__version__,
        current_year=lambda: datetime.now(ZoneInfo(app.state.settings.tz)).year,
        STATUS_LABELS=STATUS_LABELS,
        JOB_STATUS_LABELS=JOB_STATUS_LABELS,
        JOB_KIND_LABELS=JOB_KIND_LABELS,
        BUILD_UPDATE=view.BUILD_UPDATE,
        short_digest=_short_digest,
        container_key=container_key,
        skip_items=skips_mod.container_items,
        skip_kind_labels=skips_mod.KIND_LABELS,
        mailcow_skip_items=lambda result: [i for i in [skips_mod.mailcow_item(result)] if i],
        row_state=view.row_state,
        filter_state=view.filter_state,
        layout_problems=view.layout_problems,
        layout_mounts=view.layout_mounts,
        layout_anchor=view.layout_anchor,
        hint_parts=view.hint_parts,
        mailcow_state=view.mailcow_state,
        initials=view.initials,
        user_initials=view.user_initials,
        photo_url=lambda person: app.state.photos.url(person),
        Role=Role,
        can=lambda user, role: user is not None and user.role >= role,
        legacy_banner=lambda user: bool(
            user is not None and user.role == Role.ADMIN
            and app.state.settings.default_role == "admin_legacy"),
    )
    templates = Jinja2Templates(env=env)
    app.state.templates = templates
    setup_signin(app)
    setup_admin_pages(app)
    setup_routes(app)

    def render(request: Request, name: str, user: User, status_code: int = 200, **ctx):
        return templates.TemplateResponse(
            request, name, {"user": user, "csrf": csrf_token(request), **ctx},
            status_code=status_code,
        )

    def submit_job(
        kind: str, target: str, user: User, work: Callable[[JobContext], Awaitable[None]]
    ) -> int:
        """Submit a job that refreshes the dashboard afterwards. Raises BusyError."""
        job_ids: list[int] = []  # filled once the runner has accepted the job

        async def after_job() -> None:
            # The runner records the final status just after `work` returns: wait for it,
            # tell the channels, then refresh the dashboard.
            try:
                if job_ids:
                    for _ in range(200):
                        found = app.state.store.get_job(job_ids[0])
                        if found is None or found.status != "running":
                            break
                        await asyncio.sleep(0.05)
                    if found is not None and found.status != "running":
                        app.state.notifier.job_finished(
                            kind, target, found.status, user.email, found.id)
            finally:
                await run_check(app)

        async def job(ctx: JobContext) -> None:
            try:
                await work(ctx)
            finally:
                task = asyncio.create_task(after_job())
                app.state.background.add(task)
                task.add_done_callback(app.state.background.discard)
                task.add_done_callback(_log_task_failure)

        job_id = runner.submit(kind, target, user.email, job)
        job_ids.append(job_id)
        return job_id

    def submit(
        kind: str, target: str, user: User, work: Callable[[JobContext], Awaitable[None]]
    ) -> RedirectResponse:
        try:
            job_id = submit_job(kind, target, user, work)
        except BusyError:
            return _redirect("/?msg=busy")
        return _redirect(f"/jobs/{job_id}")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, msg: str = "", user: User = Depends(require_role(Role.VIEWER))):
        result = _latest(app)
        error = None
        if result is None:
            try:
                result = await run_check(app)
            except Exception as exc:  # noqa: BLE001 – show the page even if docker is down
                log.exception("Check failed while loading the dashboard")
                error = f"The check could not be completed: {str(exc) or type(exc).__name__}"
        shown, hidden = skips_mod.visible_result(result, app.state.skips) if result else (None, [])
        if shown is not None and not app.state.settings.layout_check:
            shown = dataclasses.replace(shown, layout=None)  # switched off: no badges or chip
        rules_for = app.state.rules.snapshot()
        notes = {cs.container.id: release_notes_url(cs.container, rules_for(cs.container))
                 for cs in (shown.containers if shown else [])}
        return render(
            request, "dashboard.html", user,
            result=shown,
            skipped=app.state.skips.active(),
            notes_by_id={k: v for k, v in notes.items() if v},
            has_layout=view.has_layout_data(shown),
            compose_root=app.state.settings.compose_root,
            hidden_by_id={(cs.container.id if cs else "mailcow"): sk for cs, sk in hidden},
            rows=view.by_urgency(shown) if shown else [],
            overview=view.overview(shown) if shown else None,
            attention=view.attention(shown) if shown else [],
            greeting=view.greeting(datetime.now(ZoneInfo(app.state.settings.tz))),
            first_name=view.first_name(user.name),
            flash=FLASH.get(msg),
            offers_project=functools.partial(_offers_project, result),
            error=error,
        )

    @app.post("/check", dependencies=[Depends(verify_csrf)])
    async def check_now(user: User = Depends(require_role(Role.OPERATOR))):
        try:
            await run_check(app)
        except Exception:  # noqa: BLE001
            log.exception("Check now failed")
            return _redirect("/?msg=check_failed")
        return _redirect("/")

    @app.post("/digest/send", dependencies=[Depends(verify_csrf)])
    async def digest_send(user: User = Depends(require_role(Role.ADMIN))):
        try:
            result = _latest(app) or await run_check(app)
        except Exception:  # noqa: BLE001
            log.exception("The check before sending the digest failed")
            return _redirect("/settings?msg=digest_failed#digest")
        outcome = await app.state.notifier.digest(result)  # never raises
        code = {
            "sent": "digest_sent",
            "skipped – nothing to report": "digest_nothing",
            "skipped – no channel gets the digest": "digest_unconfigured",
            "failed": "digest_failed",
        }[outcome]
        return _redirect(f"/settings?msg={code}#digest")

    @app.post("/containers/{container_id}/update", dependencies=[Depends(verify_csrf)])
    async def update_container(
        container_id: str, mode: str = Form(...), user: User = Depends(require_role(Role.OPERATOR))
    ):
        cs = _find(app, container_id)
        _require_updatable(cs)
        if mode not in ("build", "project"):
            raise HTTPException(status_code=400, detail="Unknown update mode")
        if mode == "project" and not _offers_project(_latest(app), cs):
            raise HTTPException(status_code=400, detail="The whole project can't be updated here")
        info = cs.container
        whole = mode == "project"
        target = info.project if whole else info.name
        return submit(mode, target, user, lambda ctx: update_build(ctx, info, whole_project=whole))

    @app.post("/containers/{container_id}/skip", dependencies=[Depends(verify_csrf)])
    async def skip_update(
        container_id: str, kind: str = Form(...), mode: str = Form(...),
        value: str = Form(""), reason: str = Form(""),
        user: User = Depends(require_role(Role.OPERATOR)),
    ):
        result = _latest(app)
        if container_id == "mailcow":
            offered = [i for i in [skips_mod.mailcow_item(result)] if i] if result else []
            if not offered:
                raise HTTPException(status_code=404, detail="Mailcow not found")
        else:
            offered = skips_mod.container_items(_find(app, container_id))
        if kind not in skips_mod.KINDS or mode not in ("skip", "snooze7", "snooze30"):
            raise HTTPException(status_code=400, detail="Unknown skip option")
        item = next((i for i in offered if i.kind == kind), None)
        if item is None:
            raise HTTPException(status_code=400, detail="That update is not offered")
        if value and value != item.value:
            raise HTTPException(status_code=400, detail="That update is not offered")
        reason = skips_mod.clean_reason(reason)
        note = f" – reason: {reason}" if reason else ""
        label = skips_mod.KIND_LABELS[kind].lower()
        if mode == "skip":
            app.state.skips.skip(item.container_key, kind, item.value, user.email, reason)
            summary = f"Skipped {label} {item.value} for {item.container_key}{note}"
        else:
            days = 7 if mode == "snooze7" else 30
            app.state.skips.snooze(item.container_key, kind, days, user.email, reason)
            summary = f"Snoozed {label} updates for {item.container_key} for {days} days{note}"
        app.state.store.add_audit(user.email, "updates", summary)
        return _redirect("/?msg=skipped")

    @app.post("/skips/{skip_id}/remove", dependencies=[Depends(verify_csrf)])
    async def remove_skip(skip_id: int, user: User = Depends(require_role(Role.OPERATOR))):
        skip = app.state.skips.remove(skip_id)
        if skip is None:
            raise HTTPException(status_code=404, detail="Unknown skip")
        what = "snooze" if skip.snoozed else "skip"
        app.state.store.add_audit(
            user.email, "updates",
            f"Removed {what} of {skips_mod.KIND_LABELS[skip.kind].lower()} for {skip.key}")
        return _redirect("/?msg=skip_removed")

    def _rules_page(request: Request, user: User, key: str, form=None, errors=None, status_code=200):
        cs = _find_key(app, key)
        rules = app.state.rules.effective(cs.container)
        return render(
            request, "container_rules.html", user, status_code=status_code,
            cs=cs, key=key, rules=rules,
            fields=_rule_fields(rules, form, errors or {}),
        )

    @app.get("/containers/{key:path}/rules", response_class=HTMLResponse)
    async def container_rules(request: Request, key: str,
                              user: User = Depends(require_role(Role.OPERATOR))):
        return _rules_page(request, user, key)

    @app.post("/containers/{key:path}/rules", dependencies=[Depends(verify_csrf)])
    async def container_rules_save(request: Request, key: str,
                                   user: User = Depends(require_role(Role.OPERATOR))):
        _find_key(app, key)
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        try:
            app.state.rules.save(key, form, user.email)
        except RulesError as exc:
            return _rules_page(request, user, key, form, exc.errors, status_code=400)
        return _redirect("/?msg=rules_saved")

    @app.get("/containers/{container_id}/version-preview", response_class=HTMLResponse)
    async def version_preview(
        request: Request, container_id: str, tag: str = "", user: User = Depends(require_role(Role.OPERATOR))
    ):
        cs = _find(app, container_id)
        _require_updatable(cs)
        _require_offered_tag(cs, tag)
        info = cs.container
        new_image = parse_image_ref(info.image_ref).with_tag(tag)
        change, refused = None, None
        try:
            change = await asyncio.to_thread(
                plan_tag_change, info.config_files, info.service, new_image
            )
        except EditRefused as exc:
            refused = str(exc)
        except OSError as exc:
            refused = f"The compose file could not be read ({exc.strerror or exc}) – update by hand"
        return render(
            request, "preview.html", user,
            cs=cs, tag=tag, new_image=new_image, change=change, refused=refused,
        )

    @app.post("/containers/{container_id}/version", dependencies=[Depends(verify_csrf)])
    async def update_container_version(
        container_id: str,
        tag: str = Form(...),
        sha: str = Form(...),
        user: User = Depends(require_role(Role.OPERATOR)),
    ):
        cs = _find(app, container_id)
        _require_updatable(cs)
        _require_offered_tag(cs, tag)
        info = cs.container
        new_image = parse_image_ref(info.image_ref).with_tag(tag)
        try:
            change = await asyncio.to_thread(
                plan_tag_change, info.config_files, info.service, new_image
            )
        except (EditRefused, OSError):
            log.exception("Version update of %s to %s refused", info.name, tag)
            return _redirect("/?msg=edit_refused")
        if change.file_sha256 != sha:
            # Only ever apply the exact diff the user saw in the preview.
            return _redirect("/?msg=changed")

        async def wait(i, t):
            return await wait_healthy(docker_client, i, t)

        return submit(
            "version", f"{info.name} → {tag}", user,
            lambda ctx: update_version(
                ctx, info, change, backup_dir, wait,
                health_timeout=app.state.settings.health_timeout_seconds,
                tz=ZoneInfo(app.state.settings.tz),
            ),
        )

    @app.post("/mailcow/update", dependencies=[Depends(verify_csrf)])
    async def mailcow_update(user: User = Depends(require_role(Role.ADMIN))):
        result = _latest(app)
        if result is None or result.mailcow is None:
            raise HTTPException(status_code=404, detail="Mailcow not found")
        return submit(
            "mailcow", "Mailcow", user, lambda ctx: update_mailcow(ctx, app.state.settings.mailcow_dir)
        )

    @app.post("/self/update", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
    async def self_update(request: Request, user: User = Depends(require_role(Role.ADMIN))):
        result = _latest(app)
        me = next((cs for cs in result.containers if cs.container.is_self), None) if result else None
        if me is None:
            raise HTTPException(status_code=404, detail="The dashboard container was not found")
        if not me.updatable:
            raise HTTPException(status_code=400, detail="This container can't be updated here")
        info = me.container

        async def work(ctx: JobContext) -> None:
            ctx.log("Starting a helper container to pull and recreate the dashboard\n")
            try:
                helper = await asyncio.to_thread(
                    start_self_update, docker_client, info, settings.compose_root
                )
            except (UpdateFailed, docker.errors.DockerException):
                log.exception("Could not start the dashboard self-update")
                raise
            ctx.log(
                "Helper started – the dashboard will restart shortly. After the restart"
                " this job is marked \"Dashboard restarted – self-update applied\".\n"
            )
            # Holds the runner (no other update starts) until the helper exits or
            # this dashboard is replaced.
            await follow_self_update(
                ctx, helper, timeout=SELF_RESTART_WAIT, grace=SELF_UPDATE_GRACE
            )

        try:
            job_id = submit_job("self", info.name, user, work)
        except BusyError:
            return _redirect("/?msg=busy")
        return render(
            request, "message.html", user,
            title="Updating the dashboard",
            message="Updating the dashboard – this page will refresh by itself once it's back.",
            job_id=job_id,
            await_restart=True,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: int, user: User = Depends(require_role(Role.OPERATOR))):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return render(request, "job.html", user, job=job)

    @app.get("/jobs/{job_id}/stream")
    async def job_stream(request: Request, job_id: int,
                         user: User = Depends(require_role(Role.OPERATOR))):
        cookie = request.cookies.get(COOKIE)
        session_hash = SessionStore.token_hash(cookie) if cookie else None

        def still_allowed() -> bool:
            """The stream outlives the request that opened it, so re-check the account
            and the session whenever something is about to be sent."""
            rec = app.state.users.get(user.id)
            if rec is None or rec.disabled or session_hash is None:
                return False
            try:
                if Role(rec.role) < Role.OPERATOR:
                    return False
            except ValueError:
                return False
            return store.session_get(session_hash) is not None

        async def events() -> AsyncIterator[str]:
            async for chunk in runner.subscribe(job_id, idle=STREAM_RECHECK):
                if not still_allowed():
                    return
                if chunk is None:  # idle: nothing to send, the check above was the point
                    continue
                if chunk.startswith(_END):
                    yield f"event: end\ndata: {chunk[len(_END):]}\n\n"
                    return
                lines = chunk.splitlines() or [""]
                yield "".join(f"data: {line}\n" for line in lines) + "\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def settings_fields(section: str, submitted=None, errors=None) -> list[dict]:
        """What the template needs per field. `submitted` (a re-render after an error)
        replaces non-secret values; secrets are never filled in."""
        service = app.state.settings_service
        values = service.form_values(section)
        out = []
        for f in (f for f in FIELDS if f.section == section):
            locked = service.locked(f.name)
            value = values[f.name]
            if submitted is not None and not locked and f.kind != "secret":
                value = submitted.get(f.name, "")
            out.append({
                "name": f.name, "label": f.label, "kind": f.kind, "help": f.help,
                "minimum": f.minimum, "maximum": f.maximum,
                "choices": choices_for(f, value) if f.kind == "choice" else (),
                "value": value, "locked": locked,
                "error": (errors or {}).get(f.name),
                "has_secret": f.kind == "secret" and service.has_secret(f.name),
                "unreadable": f.name in service.unreadable,
            })
        return out

    def render_settings(
        request: Request, user: User, status_code: int = 200, *, msg: str = "",
        section: str | None = None, submitted=None, errors=None,
        registry_form=None, registry_error=None, group_form=None, group_error=None,
        flash=None,
    ):
        service = app.state.settings_service
        fields = {key: settings_fields(
            key, submitted if key == section else None, errors if key == section else None,
        ) for key in SECTIONS}
        logins = service.registry_logins()  # read after the fields: it can mark them unreadable
        return render(
            request, "settings.html", user, status_code,
            sections=[(key, title, fields[key]) for key, title in SECTIONS.items()],
            registry_logins=[{"host": i.get("host", ""), "username": i.get("username", "")}
                             for i in logins],
            registry_unreadable="registry_logins" in service.unreadable,
            registry_form=registry_form or {"host": "", "username": ""},
            registry_error=registry_error,
            redirect_uri=redirect_uri(app.state.settings),
            group_mappings=service.group_mappings(),
            group_roles=MAPPABLE_ROLES,
            group_form=group_form or {"group_id": "", "role": "viewer"},
            group_error=group_error,
            channels=channel_rows(),
            channels_unreadable=app.state.channels.unreadable,
            providers=PROVIDERS,
            digest_enabled=bool(app.state.channels.subscribed("digest")),
            flash=flash or SETTINGS_FLASH.get(msg),
        )

    def channel_rows() -> list[dict]:
        rows = []
        for c in app.state.channels.list():
            provider = PROVIDERS.get(c.provider)
            last = app.state.notifier.last_result(c.id)
            status, at = "Not used yet", None
            if last is not None:
                if last.get("ok"):
                    status = "sent"
                    try:
                        at = datetime.fromisoformat(str(last.get("at")))
                    except ValueError:
                        status, at = "Not used yet", None
                else:
                    status = f"Last attempt failed – {known_reason(last.get('reason'))}"
            rows.append({
                "id": c.id, "name": c.name, "enabled": c.enabled, "readonly": c.readonly,
                "provider": provider.label if provider else c.provider,
                "recipients": ", ".join(c.recipients),
                "events": [label for key, label in EVENTS.items() if key in c.events],
                "status": status, "at": at,
                "failed": status.startswith("Last attempt failed"),
            })
        return rows

    def channel_flash(msg: str, channel: str, reason: str):
        """Test results: the name comes from the store (escaped by the template) and the
        reason from CHANNEL_REASONS by number – nothing from the query is shown."""
        if msg not in ("channel_test_ok", "channel_test_failed") or not CHANNEL_ID.match(channel):
            return None
        found = app.state.channels.get(channel)
        if found is None:
            return None
        if msg == "channel_test_ok":
            return (f"Test sent to {found.name}", "ok")
        if not (reason.isascii() and reason.isdecimal() and len(reason) <= 2
                and int(reason) < len(CHANNEL_REASONS)):
            return None
        return (f"The test couldn't be sent to {found.name} – {CHANNEL_REASONS[int(reason)]}",
                "error")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request, msg: str = "", channel: str = "", reason: str = "",
        user: User = Depends(require_role(Role.ADMIN)),
    ):
        return render_settings(request, user, msg=msg, flash=channel_flash(msg, channel, reason))

    # The fixed paths below are declared before /settings/{section} so they win.
    # -- notification channels ---------------------------------------------------------------
    def sync_digest() -> None:
        if app.state.scheduler is not None:
            from app.scheduler import sync_digest_job  # deferred: scheduler imports this module

            try:
                sync_digest_job(app)
            except Exception as exc:  # noqa: BLE001 – the change is saved; don't 500 over scheduling
                log.warning("Rescheduling the digest failed (%s)", type(exc).__name__)

    def channel_or_404(channel_id: str, *, editable: bool = True) -> Channel:
        found = app.state.channels.get(channel_id) if CHANNEL_ID.match(channel_id) else None
        if found is None or (editable and found.readonly):
            raise HTTPException(status_code=404)
        return found

    def render_channel_form(
        request: Request, user: User, provider_key: str, status_code: int = 200, *,
        channel: Channel | None = None, submitted=None, errors=None,
    ):
        provider = PROVIDERS[provider_key]
        if submitted is not None:
            values = {"name": submitted.get("name", ""), "recipients": submitted.get("recipients", ""),
                      "events": set(submitted.getlist("events")),
                      "enabled": submitted.get("enabled") == "on"}
        elif channel is not None:
            values = {"name": channel.name, "recipients": ", ".join(channel.recipients),
                      "events": set(channel.events), "enabled": channel.enabled}
        else:
            values = {"name": "", "recipients": "", "events": set(DEFAULT_EVENTS), "enabled": True}
        fields = []
        for f in provider.fields:
            if f.kind == "secret":
                value = ""
            elif submitted is not None:
                value = submitted.get(f.name, "")
            elif channel is not None and channel.provider == provider_key:
                value = channel.config.get(f.name, "")
            else:
                value = f.default
            fields.append({
                "name": f.name, "label": f.label, "kind": f.kind, "help": f.help,
                "choices": f.choices, "value": value, "error": (errors or {}).get(f.name),
                "has_secret": f.kind == "secret" and channel is not None
                and channel.provider == provider_key
                and app.state.channels.has_secret(channel.id, f.name),
            })
        return render(
            request, "channel_form.html", user, status_code,
            provider=provider, channel=channel, values=values, fields=fields,
            events=EVENTS, errors=errors or {},
            presets=SMTP_PRESETS if provider_key == "smtp" else {},
        )

    @app.get("/settings/channels/new", response_class=HTMLResponse)
    async def channel_new(request: Request, provider: str = "",
                          user: User = Depends(require_role(Role.ADMIN))):
        if provider not in PROVIDERS:
            raise HTTPException(status_code=404)
        return render_channel_form(request, user, provider)

    @app.get("/settings/channels/{channel_id}", response_class=HTMLResponse)
    async def channel_edit(channel_id: str, request: Request,
                           user: User = Depends(require_role(Role.ADMIN))):
        found = channel_or_404(channel_id)
        if found.provider not in PROVIDERS:
            raise HTTPException(status_code=404)
        return render_channel_form(request, user, found.provider, channel=found)

    async def save_channel(request: Request, user: User, existing: Channel | None):
        form = await request.form()
        provider_key = existing.provider if existing else str(form.get("provider") or "")
        if provider_key not in PROVIDERS:
            raise HTTPException(status_code=404)
        candidate = Channel(
            existing.id if existing else "", str(form.get("name") or ""), provider_key, {},
            [r for r in RECIPIENT_SPLIT.split(str(form.get("recipients") or "")) if r],
            {str(e) for e in form.getlist("events")}, form.get("enabled") == "on",
        )
        config = {f.name: str(form.get(f.name) or "") for f in PROVIDERS[provider_key].fields}
        try:
            app.state.channels.save(candidate, config, user.email)
        except ChannelError as err:
            return render_channel_form(request, user, provider_key, 400, channel=existing,
                                       submitted=form, errors=err.errors)
        sync_digest()
        return _redirect("/settings?msg=channel_saved#notifications")

    @app.post("/settings/channels", dependencies=[Depends(verify_csrf)])
    async def channel_create(request: Request, user: User = Depends(require_role(Role.ADMIN))):
        return await save_channel(request, user, None)

    @app.post("/settings/channels/reset-unreadable", dependencies=[Depends(verify_csrf)])
    async def channel_reset_unreadable(
        confirm: str = Form(""), user: User = Depends(require_role(Role.ADMIN)),
    ):
        if not confirm or not app.state.channels.reset_unreadable(user.email):
            return _redirect("/settings?msg=channel_reset_refused#notifications")
        sync_digest()
        return _redirect("/settings?msg=channel_reset#notifications")

    @app.post("/settings/channels/{channel_id}", dependencies=[Depends(verify_csrf)])
    async def channel_update(channel_id: str, request: Request,
                             user: User = Depends(require_role(Role.ADMIN))):
        return await save_channel(request, user, channel_or_404(channel_id))

    @app.post("/settings/channels/{channel_id}/delete", dependencies=[Depends(verify_csrf)])
    async def channel_delete(channel_id: str, user: User = Depends(require_role(Role.ADMIN))):
        channel_or_404(channel_id)
        try:
            app.state.channels.delete(channel_id, user.email)
        except ChannelError:
            return _redirect("/settings?msg=channel_unavailable#notifications")
        sync_digest()
        return _redirect("/settings?msg=channel_removed#notifications")

    @app.post("/settings/channels/{channel_id}/test", dependencies=[Depends(verify_csrf)])
    async def channel_test(channel_id: str, user: User = Depends(require_role(Role.ADMIN))):
        channel_or_404(channel_id, editable=False)  # the environment channel can be tested
        reason = await app.state.notifier.test(channel_id)
        if reason is None:
            return _redirect(f"/settings?msg=channel_test_ok&channel={channel_id}#notifications")
        code = CHANNEL_REASONS.index(known_reason(reason))
        return _redirect(
            f"/settings?msg=channel_test_failed&channel={channel_id}&reason={code}#notifications")

    @app.post("/settings/registries", dependencies=[Depends(verify_csrf)])
    async def settings_registry_add(
        request: Request,
        host: str = Form(""),
        username: str = Form(""),
        password: str = Form(""),
        user: User = Depends(require_role(Role.ADMIN)),
    ):
        def refuse(message: str):
            return render_settings(
                request, user, 400, registry_error=message,
                registry_form={"host": host.strip(), "username": username.strip()},
            )

        try:
            registry = normalise_host(host)
        except ValueError:
            return refuse("Enter a registry host, for example ghcr.io")
        username = username.strip()
        if not username or not password:
            return refuse("Enter a username and password")
        try:
            await test_login(app.state.http, registry, username, password)
        except RegistryError as exc:  # fixed messages only
            return refuse(str(exc))
        service = app.state.settings_service
        logins = [i for i in service.registry_logins() if i.get("host") != registry]
        logins.append({"host": registry, "username": username, "password": password})
        try:
            service.save_registry_logins(logins, user.email, f"Registry login {registry} added")
        except KeyUnusable:
            return refuse(KEY_UNUSABLE_MESSAGE)
        apply_settings(app)
        return _redirect("/settings?msg=registry_saved#registries")

    @app.post("/settings/registries/{host:path}/delete", dependencies=[Depends(verify_csrf)])
    async def settings_registry_delete(host: str, user: User = Depends(require_role(Role.ADMIN))):
        try:
            registry = normalise_host(host)
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown registry") from None
        service = app.state.settings_service
        logins = service.registry_logins()
        kept = [i for i in logins if i.get("host") != registry]
        if len(kept) != len(logins):
            service.save_registry_logins(kept, user.email, f"Registry login {registry} removed")
            apply_settings(app)
        return _redirect("/settings?msg=registry_removed#registries")

    @app.post("/settings/signin/test", dependencies=[Depends(verify_csrf)])
    async def settings_signin_test(user: User = Depends(require_role(Role.ADMIN))):
        ok = await check_settings(app.state.http, app.state.settings)
        return _redirect(f"/settings?msg={'entra_ok' if ok else 'entra_failed'}#signin")

    @app.post("/settings/group-mappings", dependencies=[Depends(verify_csrf)])
    async def settings_group_add(
        request: Request,
        group_id: str = Form(""),
        role: str = Form(""),
        user: User = Depends(require_role(Role.ADMIN)),
    ):
        service = app.state.settings_service
        gid = group_id.strip().lower()
        mappings = [m for m in service.group_mappings() if m["group_id"].lower() != gid]
        mappings.append({"group_id": gid, "role": role})
        try:
            service.save_group_mappings(
                mappings, user.email, f"Group {gid} mapped to {MAPPABLE_ROLES.get(role, role)}")
        except ValueError as exc:  # fixed messages only
            return render_settings(
                request, user, 400, group_error=str(exc),
                group_form={"group_id": group_id.strip(), "role": role})
        return _redirect("/settings?msg=group_saved#signin")

    @app.post("/settings/group-mappings/{group_id}/delete", dependencies=[Depends(verify_csrf)])
    async def settings_group_delete(group_id: str, user: User = Depends(require_role(Role.ADMIN))):
        service = app.state.settings_service
        gid = group_id.strip().lower()
        mappings = service.group_mappings()
        kept = [m for m in mappings if m["group_id"].lower() != gid]
        if len(kept) != len(mappings):
            service.save_group_mappings(kept, user.email, f"Group {gid} role removed")
        return _redirect("/settings?msg=group_removed#signin")

    @app.post("/settings/{section}", dependencies=[Depends(verify_csrf)])
    async def settings_save(
        request: Request, section: str, user: User = Depends(require_role(Role.ADMIN))
    ):
        if section not in SECTIONS:
            raise HTTPException(status_code=404, detail="Unknown settings section")
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        try:
            app.state.settings_service.save(section, form, user.email)
        except SettingsError as exc:
            return render_settings(
                request, user, 400, section=section, submitted=form, errors=exc.errors
            )
        apply_settings(app)
        return _redirect(f"/settings?msg=saved#{section}")

    @app.get("/history", response_class=HTMLResponse)
    async def history(request: Request, view: str = "", kind: str = "",
                      user: User = Depends(require_role(Role.VIEWER))):
        if view == "changes":
            if user.role < Role.ADMIN:
                raise RoleDenied()
            kind = kind if kind in CHANGE_KINDS else ""  # unknown: show everything
            return render(request, "history.html", user, view="changes", kind=kind,
                          change_kinds=CHANGE_KINDS, changes=store.list_audit(kind=kind or None))
        return render(request, "history.html", user, view="updates", jobs=store.list_jobs())

    @app.get("/layout", response_class=HTMLResponse)
    async def layout_page(request: Request, user: User = Depends(require_role(Role.VIEWER))):
        settings = app.state.settings
        result = _latest(app)
        problems = result.layout if (result and settings.layout_check) else None
        return render(
            request, "layout.html", user,
            enabled=settings.layout_check, checked=problems is not None,
            compose_root=settings.compose_root,
            groups=view.layout_groups(problems or []),
        )

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz():
        """Liveness for the container HEALTHCHECK: public, no detail, never cached."""
        if app.state.store.ping():
            body, status = {"status": "ok"}, 200
        else:
            body, status = {"status": "unavailable"}, 503
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    return app


class HealthzAccessFilter(logging.Filter):
    """Drop uvicorn access-log lines for /healthz so the 30-second probe doesn't fill the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            return args[2].split("?", 1)[0] != "/healthz"
        return True


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx logs every request URL at INFO, and the heartbeat URL carries a secret ping token.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthzAccessFilter) for f in access.filters):
        access.addFilter(HealthzAccessFilter())


def build_app() -> FastAPI:
    """Factory for `uvicorn --factory app.main:build_app`."""
    configure_logging()
    return create_app(get_settings())
