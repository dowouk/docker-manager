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
from app.digest import send_digest
from app.imageref import parse_image_ref
from app.inventory import detect_self_id, list_containers
from app.jobs import BUSY_MESSAGE, BusyError, JobContext, JobRunner
from app.mailcow import check_mailcow, run_capture, update_mailcow
from app.registry import RegistryClient
from app.store import Store
from app import view
from app.updaters import (
    UpdateFailed,
    follow_self_update,
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


def create_app(
    settings: Settings,
    *,
    docker_client=None,
    http: httpx.AsyncClient | None = None,
    store: Store | None = None,
    runner: JobRunner | None = None,
    start_scheduler: bool = True,
    email_client_factory=None,
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        if start_scheduler:
            from app.scheduler import build_scheduler  # deferred: scheduler imports this module

            scheduler = build_scheduler(app, settings)
            scheduler.start()
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
    app.state.settings = settings
    app.state.docker = docker_client
    app.state.http = http
    app.state.registry = RegistryClient(http, settings.registry_auth_file)
    app.state.store = store
    app.state.runner = runner
    app.state.check_lock = asyncio.Lock()
    app.state.background = set()
    app.state.email_client_factory = email_client_factory
    backup_dir = os.path.join(settings.data_dir, "backups")
    tz = ZoneInfo(settings.tz)

    setup_auth(app, settings)
    app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(_HERE, "templates")),
        autoescape=True,
    )

    def uk_date(dt: datetime | None) -> str:
        if dt is None:
            return "–"
        local = dt.astimezone(tz)
        return f"{local.day} {local:%b %Y, %H:%M}"

    env.filters["uk_date"] = uk_date
    env.globals.update(
        server_name=settings.server_name,
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
            greeting=view.greeting(datetime.now(tz)),
            first_name=view.first_name(user.name),
            flash=FLASH.get(msg),
            offers_project=functools.partial(_offers_project, result),
            digest_enabled=settings.digest_enabled,
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
            outcome = await asyncio.to_thread(
                send_digest, settings, result, app.state.email_client_factory
            )
        except Exception:  # noqa: BLE001
            log.exception("Sending the digest failed")
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
                health_timeout=settings.health_timeout_seconds, tz=tz,
            ),
        )

    @app.post("/mailcow/update", dependencies=[Depends(verify_csrf)])
    async def mailcow_update(user: User = Depends(current_user)):
        result = _latest(app)
        if result is None or result.mailcow is None:
            raise HTTPException(status_code=404, detail="Mailcow not found")
        return submit(
            "mailcow", "Mailcow", user, lambda ctx: update_mailcow(ctx, settings.mailcow_dir)
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

    @app.get("/history", response_class=HTMLResponse)
    async def history(request: Request, user: User = Depends(current_user)):
        return render(request, "history.html", user, jobs=store.list_jobs())

    return app


def build_app() -> FastAPI:
    """Factory for `uvicorn --factory app.main:build_app`."""
    logging.basicConfig(level=logging.INFO)
    return create_app(get_settings())
