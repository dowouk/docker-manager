"""FastAPI app: dashboard pages, update actions and live job logs."""
import asyncio
import functools
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import docker
import docker.errors
import httpx
import jinja2
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import User, csrf_token, current_user, setup_auth, verify_csrf
from app.checker import CheckResult, ContainerStatus, Status, check_all
from app.compose_edit import EditRefused, plan_tag_change
from app.config import Settings, get_settings
from app.digest import EmailNotConfigured, send_digest, send_test_email
from app.imageref import parse_image_ref
from app.inventory import detect_self_id, list_containers
from app.jobs import BUSY_MESSAGE, BusyError, JobContext, JobRunner, set_docker_config_dir
from app.mailcow import check_mailcow, run_capture, update_mailcow
from app.keys import KeyUnusable, load_secret_box
from app.registry import RegistryClient, RegistryError
from app.registry_logins import (
    logins_by_host,
    normalise_host,
    test_login,
    write_docker_config,
)
from app.sessions import SessionStore
from app.settings_service import (
    FIELDS, KEY_UNUSABLE_MESSAGE, _choices_for, SECTIONS, SettingsError, SettingsService, key_path,
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

# Flash messages: only these fixed strings are ever shown (no reflected text).
# Each is (text, level); "ok" shows as a status note, "error" as an alert.
FLASH = {
    "busy": (BUSY_MESSAGE, "error"),
    "check_failed": ("The check could not be completed – see the server logs", "error"),
    "edit_refused": ("The compose file can't be edited automatically – update by hand", "error"),
    "changed": ("The compose file changed since the preview – review the change again", "error"),
    "digest_sent": ("Digest sent", "ok"),
    "digest_nothing": ("Digest skipped – nothing to report", "ok"),
    "digest_unconfigured": ("Digest skipped – email not configured", "ok"),
    "digest_failed": ("The digest could not be sent – check the server logs", "error"),
}

# Same idea for the Settings page.
SETTINGS_FLASH = {
    "saved": ("Settings saved", "ok"),
    "email_sent": ("Test email sent", "ok"),
    "email_failed": ("The test email could not be sent – check the server logs", "error"),
    "email_unconfigured": ("Save a connection string, sender and recipient before sending a test", "error"),
    "registry_saved": ("Registry login saved", "ok"),
    "registry_removed": ("Registry login removed", "ok"),
}

STATUS_LABELS = {
    Status.UP_TO_DATE: "Up to date",
    Status.NEWER_BUILD: "Newer build available",
    Status.PULLED_NOT_RECREATED: "Pulled – not recreated",
    Status.NOT_CHECKABLE: "Can't check",
    Status.ERROR: "Error",
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
    async with state.check_lock:
        await asyncio.to_thread(remove_finished_helpers, state.docker)
        containers = await asyncio.to_thread(list_containers, state.docker, detect_self_id())
        result = await check_all(
            containers,
            state.registry,
            state.settings,
            mailcow_check=functools.partial(
                check_mailcow, state.settings.mailcow_dir, run_capture, state.http
            ),
        )
        state.store.save_check(result.to_dict(), result.checked_at)
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
    email_client_factory=None,
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
    app.state.email_client_factory = email_client_factory
    backup_dir = os.path.join(settings.data_dir, "backups")

    app.state.sessions = SessionStore(store)
    app.state.sessions.purge_expired()  # dead rows would otherwise pile up
    setup_auth(app, settings, app.state.sessions)
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
    env.globals.update(
        server_name=app.state.settings.server_name,
        STATUS_LABELS=STATUS_LABELS,
        JOB_STATUS_LABELS=JOB_STATUS_LABELS,
        JOB_KIND_LABELS=JOB_KIND_LABELS,
        BUILD_UPDATE=view.BUILD_UPDATE,
        short_digest=_short_digest,
        row_state=view.row_state,
        mailcow_state=view.mailcow_state,
        initials=view.initials,
        user_initials=view.user_initials,
    )
    templates = Jinja2Templates(env=env)
    app.state.templates = templates

    def render(request: Request, name: str, user: User, status_code: int = 200, **ctx):
        return templates.TemplateResponse(
            request, name, {"user": user, "csrf": csrf_token(request), **ctx},
            status_code=status_code,
        )

    def submit_job(
        kind: str, target: str, user: User, work: Callable[[JobContext], Awaitable[None]]
    ) -> int:
        """Submit a job that refreshes the dashboard afterwards. Raises BusyError."""
        async def job(ctx: JobContext) -> None:
            try:
                await work(ctx)
            finally:
                task = asyncio.create_task(run_check(app))
                app.state.background.add(task)
                task.add_done_callback(app.state.background.discard)
                task.add_done_callback(_log_task_failure)

        return runner.submit(kind, target, user.email, job)

    def submit(
        kind: str, target: str, user: User, work: Callable[[JobContext], Awaitable[None]]
    ) -> RedirectResponse:
        try:
            job_id = submit_job(kind, target, user, work)
        except BusyError:
            return _redirect("/?msg=busy")
        return _redirect(f"/jobs/{job_id}")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, msg: str = "", user: User = Depends(current_user)):
        result = _latest(app)
        error = None
        if result is None:
            try:
                result = await run_check(app)
            except Exception as exc:  # noqa: BLE001 – show the page even if docker is down
                log.exception("Check failed while loading the dashboard")
                error = f"The check could not be completed: {str(exc) or type(exc).__name__}"
        return render(
            request, "dashboard.html", user,
            result=result,
            rows=view.by_urgency(result) if result else [],
            overview=view.overview(result) if result else None,
            attention=view.attention(result) if result else [],
            greeting=view.greeting(datetime.now(ZoneInfo(app.state.settings.tz))),
            first_name=view.first_name(user.name),
            flash=FLASH.get(msg),
            offers_project=functools.partial(_offers_project, result),
            digest_enabled=app.state.settings.digest_enabled,
            error=error,
        )

    @app.post("/check", dependencies=[Depends(verify_csrf)])
    async def check_now(user: User = Depends(current_user)):
        try:
            await run_check(app)
        except Exception:  # noqa: BLE001
            log.exception("Check now failed")
            return _redirect("/?msg=check_failed")
        return _redirect("/")

    @app.post("/digest/send", dependencies=[Depends(verify_csrf)])
    async def digest_send(user: User = Depends(current_user)):
        try:
            result = _latest(app) or await run_check(app)
        except Exception:  # noqa: BLE001
            log.exception("The check before sending the digest failed")
            return _redirect("/?msg=digest_failed")
        try:
            outcome = await asyncio.to_thread(
                send_digest, app.state.settings, result, app.state.email_client_factory
            )
        except Exception as exc:  # noqa: BLE001
            # Type only: the message can contain the email connection string.
            log.error("Sending the digest failed (%s)", type(exc).__name__)
            return _redirect("/?msg=digest_failed")
        code = {
            "sent": "digest_sent",
            "skipped – nothing to report": "digest_nothing",
            "skipped – email not configured": "digest_unconfigured",
        }[outcome]
        return _redirect(f"/?msg={code}")

    @app.post("/containers/{container_id}/update", dependencies=[Depends(verify_csrf)])
    async def update_container(
        container_id: str, mode: str = Form(...), user: User = Depends(current_user)
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

    @app.get("/containers/{container_id}/version-preview", response_class=HTMLResponse)
    async def version_preview(
        request: Request, container_id: str, tag: str = "", user: User = Depends(current_user)
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
        user: User = Depends(current_user),
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
    async def mailcow_update(user: User = Depends(current_user)):
        result = _latest(app)
        if result is None or result.mailcow is None:
            raise HTTPException(status_code=404, detail="Mailcow not found")
        return submit(
            "mailcow", "Mailcow", user, lambda ctx: update_mailcow(ctx, app.state.settings.mailcow_dir)
        )

    @app.post("/self/update", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
    async def self_update(request: Request, user: User = Depends(current_user)):
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
            message="Updating the dashboard – reload in a minute",
            job_id=job_id,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: int, user: User = Depends(current_user)):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return render(request, "job.html", user, job=job)

    @app.get("/jobs/{job_id}/stream")
    async def job_stream(job_id: int, user: User = Depends(current_user)):
        async def events() -> AsyncIterator[str]:
            async for chunk in runner.subscribe(job_id):
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
                "choices": _choices_for(f, value) if f.kind == "choice" else (),
                "value": value, "locked": locked,
                "error": (errors or {}).get(f.name),
                "has_secret": f.kind == "secret" and service.has_secret(f.name),
                "unreadable": f.name in service.unreadable,
            })
        return out

    def render_settings(
        request: Request, user: User, status_code: int = 200, *, msg: str = "",
        section: str | None = None, submitted=None, errors=None,
        registry_form=None, registry_error=None,
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
            flash=SETTINGS_FLASH.get(msg),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, msg: str = "", user: User = Depends(current_user)):
        return render_settings(request, user, msg=msg)

    # The fixed paths below are declared before /settings/{section} so they win.
    @app.post("/settings/email/test", dependencies=[Depends(verify_csrf)])
    async def settings_email_test(user: User = Depends(current_user)):
        try:
            await asyncio.to_thread(
                send_test_email, app.state.settings, app.state.email_client_factory
            )
        except EmailNotConfigured:
            return _redirect("/settings?msg=email_unconfigured#email")
        except Exception as exc:  # noqa: BLE001 – the text can hold the connection string: log the type only
            log.error("Sending the test email failed (%s)", type(exc).__name__)
            return _redirect("/settings?msg=email_failed#email")
        return _redirect("/settings?msg=email_sent#email")

    @app.post("/settings/registries", dependencies=[Depends(verify_csrf)])
    async def settings_registry_add(
        request: Request,
        host: str = Form(""),
        username: str = Form(""),
        password: str = Form(""),
        user: User = Depends(current_user),
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
    async def settings_registry_delete(host: str, user: User = Depends(current_user)):
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

    @app.post("/settings/{section}", dependencies=[Depends(verify_csrf)])
    async def settings_save(
        request: Request, section: str, user: User = Depends(current_user)
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
    async def history(request: Request, view: str = "", user: User = Depends(current_user)):
        if view == "changes":
            return render(request, "history.html", user, view="changes",
                          changes=store.list_audit())
        return render(request, "history.html", user, view="updates", jobs=store.list_jobs())

    return app


def build_app() -> FastAPI:
    """Factory for `uvicorn --factory app.main:build_app`."""
    logging.basicConfig(level=logging.INFO)
    return create_app(get_settings())
