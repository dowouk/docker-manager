"""Microsoft Entra ID sign-in (OIDC code flow + PKCE), auth guard and CSRF.

The guard is a pure ASGI middleware that sits *inside* ServerSessionMiddleware, so
``scope["session"]`` is available. Everything except ``/auth/*``, ``/static/*``,
``/healthz`` (and ``/setup*`` while setup is open) needs a signed-in, enabled user,
loaded from the database on every request. There is no fallback mode.
"""
import secrets
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from authlib.common.errors import AuthlibBaseError
from joserfc.errors import JoseError
from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.config import Settings
from app.roles import Role
from app.sessions import ServerSessionMiddleware, SessionStore
from app.users import UserStore

_PUBLIC_PREFIXES = ("/auth/", "/static/")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class Identity:
    """Who a verified Entra token says signed in (not yet an account)."""
    name: str
    email: str


@dataclass(frozen=True)
class User:
    id: int
    name: str
    email: str
    role: Role
    kind: str


def _issuer(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0"


def user_from_claims(claims: dict, settings: Settings) -> Identity | None:
    """Return the identity if the (already signature-verified) claims are acceptable.

    Fails closed: any configured restriction (ALLOWED_EMAILS, ALLOWED_GROUP_ID)
    must be satisfied. If groups are missing from the token (e.g. group overage)
    and a group restriction is configured, access is denied.
    """
    if not isinstance(claims, dict):
        return None
    if claims.get("tid") != settings.azure_tenant_id:
        return None
    if claims.get("iss") != _issuer(settings):
        return None
    aud = claims.get("aud")
    if isinstance(aud, list):
        # multi-audience token: the authorised party must be this client too
        if settings.azure_client_id not in aud or claims.get("azp") != settings.azure_client_id:
            return None
    elif aud != settings.azure_client_id:
        return None

    def _clean(value) -> str:
        return value.strip() if isinstance(value, str) else ""

    username = _clean(claims.get("preferred_username"))
    email = _clean(claims.get("email"))
    identity = username or email
    if not identity:
        return None

    if settings.allowed_emails:
        # preferred_username only: the `email` claim may be unverified (guests)
        allowed = {e.strip().lower() for e in settings.allowed_emails if e and e.strip()}
        if not username or username.lower() not in allowed:
            return None
    if settings.allowed_group_id:
        groups = claims.get("groups")
        if not isinstance(groups, list) or settings.allowed_group_id not in groups:
            return None

    return Identity(name=_clean(claims.get("name")) or identity, email=identity)


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


class AuthGuardMiddleware:
    """Reject every request without a signed-in, enabled user.

    The user is loaded from the database on every request, so a disabled account,
    a deleted account or a changed role takes effect on the very next request.
    While first-start setup is open every other path leads to /setup.
    """

    def __init__(self, app, users_getter: Callable[[], UserStore],
                 setup_open: Callable[[], bool] = lambda: False):
        self.app = app
        self.users_getter = users_getter
        self.setup_open = setup_open

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
        user = self._load(session)
        if user is None:
            if isinstance(session, dict):
                session.clear()  # persisted as a sign-out by the session middleware
            await self._refuse(scope, receive, send, "/auth/login")
            return
        if user.role == Role.PENDING and not _pending_may_visit(path):
            await self._refuse(scope, receive, send, "/pending", status=403)
            return
        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)

    def _load(self, session) -> User | None:
        user_id = session.get("user_id") if isinstance(session, dict) else None
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None
        rec = self.users_getter().get(user_id)
        if rec is None or rec.disabled:
            return None
        try:
            role = Role(rec.role)
        except ValueError:
            return None
        return User(id=rec.id, name=rec.name, email=rec.email, role=role, kind=rec.kind)

    async def _refuse(self, scope, receive, send, location: str, status: int = 401):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        accept = headers.get(b"accept", b"").decode("latin-1").lower()
        if scope["method"] in ("GET", "HEAD") and "text/html" in accept:
            response: Response = RedirectResponse(location, status_code=302)
        elif status == 403:
            response = JSONResponse({"detail": "Your account is waiting for approval"}, status_code=403)
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
    oauth = OAuth()
    if not settings.entra_enabled:
        # Task 7 replaces this; until then Microsoft sign-in is simply unavailable.
        app.state.oauth = oauth

        @app.get("/auth/login", include_in_schema=False)
        @app.get("/auth/callback", include_in_schema=False)
        async def not_set_up():
            if (settings.azure_tenant_id and settings.azure_client_id
                    and settings.azure_client_secret):
                return _page("Microsoft sign-in isn't set up",
                             "Set the dashboard address on the Settings page first.", 503)
            return _page("Microsoft sign-in isn't set up",
                         "Ask the administrator to set up Microsoft sign-in.", 503)

        _finish_setup(app, sessions, setup_open)
        return
    oauth.register(
        name="entra",
        client_id=settings.azure_client_id,
        client_secret=settings.azure_client_secret,
        server_metadata_url=(
            f"https://login.microsoftonline.com/{settings.azure_tenant_id}"
            "/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    app.state.oauth = oauth
    redirect_uri = settings.public_url.rstrip("/") + "/auth/callback"

    @app.get("/auth/login", include_in_schema=False)
    async def login(request: Request):
        return await oauth.entra.authorize_redirect(request, redirect_uri)

    @app.get("/auth/callback", include_in_schema=False)
    async def callback(request: Request):
        try:
            token = await oauth.entra.authorize_access_token(request)
        except (OAuthError, AuthlibBaseError, JoseError, httpx.HTTPError):
            request.session.clear()
            return _page(
                "Sign-in failed",
                "Sign-in could not be completed. <a href=\"/auth/login\">Try again</a>.",
                400,
            )
        identity = user_from_claims(token.get("userinfo") or {}, settings)
        request.session.clear()  # always start fresh (session fixation)
        users: UserStore = request.app.state.users
        # Interim (until Task 7): 0.2 access rules, and 0.2 gave every allowed user full
        # control, so a first sign-in creates an Admin.
        record = None
        if identity is not None:
            record = users.by_email("entra", identity.email) or users.create(
                "entra", identity.email, identity.name, Role.ADMIN.value, pinned=True)
        if record is None or record.disabled:
            return _page(
                "You don't have access to this dashboard",
                "Your account is not permitted to use this dashboard. "
                "Ask the administrator for access.",
                403,
            )
        users.touch_sign_in(record.id)
        request.session["user_id"] = record.id
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", status_code=302)

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
                       setup_open=setup_open)
    app.add_middleware(ServerSessionMiddleware, sessions=sessions)
