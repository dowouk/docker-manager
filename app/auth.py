"""Auth guard, roles on routes and CSRF (Microsoft sign-in is in app.entra).

The guard is a pure ASGI middleware that sits *inside* ServerSessionMiddleware, so
``scope["session"]`` is available. Everything except ``/auth/*``, ``/static/*``,
``/healthz`` (and ``/setup*`` while setup is open) needs a signed-in, enabled user,
loaded from the database on every request. There is no fallback mode.
"""
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.config import Settings
from app.roles import Role
from app.sessions import ServerSessionMiddleware, SessionStore
from app.users import UserRecord, UserStore

_PUBLIC_PREFIXES = ("/auth/", "/static/")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class User:
    id: int
    name: str
    email: str
    role: Role
    kind: str


def _clean_path(path: str) -> bool:
    return ".." not in path.split("/") and "\\" not in path and "\x00" not in path


def _is_public_path(path: str, setup_open: bool = False) -> bool:
    if not _clean_path(path):
        return False
    if path == "/healthz" or path.startswith(_PUBLIC_PREFIXES):
        return True
    return setup_open and (path == "/setup" or path.startswith("/setup/"))


def _pending_may_visit(path: str) -> bool:
    return path in ("/pending", "/account") or path.startswith("/account/")


PASSWORD_PAGE = "/account/password"
TWO_STEP_PAGE = "/account/two-step"


def forced_page(rec: UserRecord, settings) -> str | None:
    """The one page this user must finish before using anything else, if any.

    Computed from the live record on every request, never from a session flag.
    """
    if rec.kind == "local" and rec.must_change_password:
        return PASSWORD_PAGE
    if (rec.kind == "local" and rec.role == Role.ADMIN.value and not rec.totp_enabled
            and settings is not None and settings.require_totp_admins):
        return TWO_STEP_PAGE
    return None


class AuthGuardMiddleware:
    """Reject every request without a signed-in, enabled user.

    The user is loaded from the database on every request, so a disabled account,
    a deleted account or a changed role takes effect on the very next request.
    While first-start setup is open every other path leads to /setup.
    """

    def __init__(self, app, users_getter: Callable[[], UserStore],
                 setup_open: Callable[[], bool] = lambda: False,
                 settings_getter: Callable[[], object] = lambda: None):
        self.app = app
        self.users_getter = users_getter
        self.setup_open = setup_open
        self.settings_getter = settings_getter

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_setup = self.setup_open()
        if _is_public_path(path, is_setup):
            await self.app(scope, receive, send)
            return
        if is_setup and _clean_path(path):
            await self._refuse(scope, receive, send, "/setup")
            return
        session = scope.get("session")
        user, rec = self._load(session)
        if user is None:
            if isinstance(session, dict) and session.get("user_id") is not None:
                # Signed-in session whose user is gone/disabled: persisted as a sign-out.
                # Anonymous sessions (OAuth state, CSRF) are left alone.
                session.clear()
            await self._refuse(scope, receive, send, "/auth/login")
            return
        forced = forced_page(rec, self.settings_getter())
        if forced is not None and path != forced:
            await self._refuse(scope, receive, send, forced, status=403,
                               detail="Finish setting up your account first")
            return
        if user.role == Role.PENDING and not _pending_may_visit(path):
            await self._refuse(scope, receive, send, "/pending", status=403)
            return
        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)

    def _load(self, session) -> tuple[User, UserRecord] | tuple[None, None]:
        user_id = session.get("user_id") if isinstance(session, dict) else None
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None, None
        rec = self.users_getter().get(user_id)
        if rec is None or rec.disabled:
            return None, None
        try:
            role = Role(rec.role)
        except ValueError:
            return None, None
        return User(id=rec.id, name=rec.name, email=rec.email, role=role, kind=rec.kind), rec

    async def _refuse(self, scope, receive, send, location: str, status: int = 401,
                      detail: str | None = None):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        accept = headers.get(b"accept", b"").decode("latin-1").lower()
        if scope["method"] in ("GET", "HEAD") and "text/html" in accept:
            response: Response = RedirectResponse(location, status_code=302)
        elif status == 403:
            response = JSONResponse(
                {"detail": detail or "Your account is waiting for approval"}, status_code=403)
        else:
            response = JSONResponse({"detail": "Not signed in"}, status_code=401)
        await response(scope, receive, send)


def current_user(request: Request) -> User:
    user = request.scope.get("state", {}).get("user")
    if not isinstance(user, User):
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


class RoleDenied(Exception):
    """The signed-in user's role is below what the route needs."""


def require_role(minimum: Role) -> Callable[[Request], User]:
    def dependency(request: Request) -> User:
        user = current_user(request)
        if user.role < minimum:
            raise RoleDenied()
        return user

    return dependency


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            value = form.get("csrf_token")
            supplied = value if isinstance(value, str) else None
    if (
        not expected
        or not supplied
        or not secrets.compare_digest(supplied.encode(), str(expected).encode())
    ):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


_PAGE = (
    "<!doctype html><html lang=\"en-GB\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>{title}</title></head><body><main><h1>{title}</h1><p>{body}</p></main></body></html>"
)


_PENDING_PAGE = (
    "<!doctype html><html lang=\"en-GB\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>Waiting for approval</title></head><body><main><h1>Waiting for approval</h1>"
    "<p>Your account is waiting for an Admin to approve it.</p>"
    "<form method=\"post\" action=\"/auth/logout\">"
    "<input type=\"hidden\" name=\"csrf_token\" value=\"{csrf}\">"
    "<button type=\"submit\">Sign out</button></form></main></body></html>"
)


def _page(title: str, body: str, status: int) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=title, body=body), status_code=status)


def setup_auth(app: FastAPI, settings: Settings, sessions: SessionStore,
               setup_open: Callable[[], bool] = lambda: False) -> None:
    """The guard, session middleware, sign-out and the pending page (Microsoft sign-in
    itself lives in app.entra)."""
    _finish_setup(app, sessions, setup_open)


def _finish_setup(app: FastAPI, sessions: SessionStore,
                  setup_open: Callable[[], bool]) -> None:
    @app.post("/auth/logout", include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/auth/signed-out", status_code=302)

    @app.get("/auth/signed-out", include_in_schema=False)
    async def signed_out():
        return _page(
            "Signed out",
            "You have been signed out. <a href=\"/auth/login\">Sign in again</a>.",
            200,
        )

    @app.get("/pending", include_in_schema=False)
    async def pending(request: Request):
        user = current_user(request)
        if user.role != Role.PENDING:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(_PENDING_PAGE.format(csrf=csrf_token(request)))

    @app.exception_handler(RoleDenied)
    async def role_denied(request: Request, exc: RoleDenied):
        accept = request.headers.get("accept", "").lower()
        if request.method in ("GET", "HEAD") and "text/html" in accept:
            return _page("You don't have permission to do that",
                         "Ask an Admin if you need access. <a href=\"/\">Back to the dashboard</a>.",
                         403)
        return JSONResponse({"detail": "You don't have permission to do that"}, status_code=403)

    # Added first = innermost: ServerSessionMiddleware (added next) wraps the guard.
    app.add_middleware(AuthGuardMiddleware, users_getter=lambda: app.state.users,
                       setup_open=setup_open,
                       settings_getter=lambda: getattr(app.state, "settings", None))
    app.add_middleware(ServerSessionMiddleware, sessions=sessions)
