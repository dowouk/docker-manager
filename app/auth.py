"""Microsoft Entra ID sign-in (OIDC code flow + PKCE), auth guard and CSRF.

The guard is a pure ASGI middleware that sits *inside* ServerSessionMiddleware, so
``scope["session"]`` is available. Everything except ``/auth/*`` and
``/static/*`` requires a signed-in session. There is no fallback mode.
"""
import secrets
from dataclasses import dataclass

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from authlib.common.errors import AuthlibBaseError
from joserfc.errors import JoseError
from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.config import Settings
from app.sessions import ServerSessionMiddleware, SessionStore

_PUBLIC_PREFIXES = ("/auth/", "/static/")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class User:
    name: str
    email: str


def _issuer(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0"


def user_from_claims(claims: dict, settings: Settings) -> User | None:
    """Return the User if the (already signature-verified) claims are acceptable.

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

    return User(name=_clean(claims.get("name")) or identity, email=identity)


def _is_public_path(path: str) -> bool:
    if ".." in path.split("/") or "\\" in path or "\x00" in path:
        return False
    return path.startswith(_PUBLIC_PREFIXES)


class AuthGuardMiddleware:
    """Reject every request without a signed-in session, except /auth/* and /static/*."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if _is_public_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        session = scope.get("session")
        user = session.get("user") if isinstance(session, dict) else None
        if isinstance(user, dict) and user.get("email"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        accept = headers.get(b"accept", b"").decode("latin-1").lower()
        if scope["method"] in ("GET", "HEAD") and "text/html" in accept:
            response: Response = RedirectResponse("/auth/login", status_code=302)
        else:
            response = JSONResponse({"detail": "Not signed in"}, status_code=401)
        await response(scope, receive, send)


def current_user(request: Request) -> User:
    user = request.session.get("user")
    if not isinstance(user, dict) or not user.get("email"):
        raise HTTPException(status_code=401, detail="Not signed in")
    return User(name=user.get("name", ""), email=user["email"])


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


def _page(title: str, body: str, status: int) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=title, body=body), status_code=status)


def setup_auth(app: FastAPI, settings: Settings, sessions: SessionStore) -> None:
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

        _finish_setup(app, sessions)
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
        user = user_from_claims(token.get("userinfo") or {}, settings)
        request.session.clear()  # always start fresh (session fixation)
        if user is None:
            return _page(
                "You don't have access to this dashboard",
                "Your account is not permitted to use this dashboard. "
                "Ask the administrator for access.",
                403,
            )
        request.session["user"] = {"name": user.name, "email": user.email}
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", status_code=302)

    _finish_setup(app, sessions)


def _finish_setup(app: FastAPI, sessions: SessionStore) -> None:
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

    # Added first = innermost: ServerSessionMiddleware (added next) wraps the guard.
    app.add_middleware(AuthGuardMiddleware)
    app.add_middleware(ServerSessionMiddleware, sessions=sessions)
