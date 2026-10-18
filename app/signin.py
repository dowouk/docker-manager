"""Local sign-in: password step, two-step (TOTP / recovery code) step, the forced
password change and authenticator enrolment.

An anonymous sign-in page session holds only ``csrf`` (plus ``pending_user_id`` and
``pending_at`` between the password and the code). ``complete_sign_in`` replaces it
with a signed-in session, which rotates the cookie.
"""
import secrets
from datetime import timedelta

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import totp
from app.auth import (
    PASSWORD_PAGE, TWO_STEP_PAGE, User, csrf_token, current_user, forced_page, verify_csrf,
)
from app.passwords import (
    DUMMY_HASH, hash_password, needs_rehash, password_problem, verify_password,
)
from app.sessions import COOKIE, SessionStore
from app.users import UserRecord, UserStore

ACCOUNT_LIMIT = 5
IP_LIMIT = 20
WINDOW = timedelta(minutes=15)
PENDING_TTL = timedelta(minutes=5)
LOCKED_MESSAGE = "Too many attempts – try again in 15 minutes."
BAD_CREDENTIALS = "Email or password is wrong"
BAD_CODE = "That code isn't right"
NO_STORE = {"Cache-Control": "no-store"}


def _key(email: str) -> str:
    return email.strip().lower()


def _ip(request: Request) -> str:
    # Best effort only: forwarded headers can be spoofed, so the per-account lock
    # is the real protection.
    return request.client.host if request.client else "unknown"


def _locked(users: UserStore, request: Request, key: str) -> bool:
    return (users.failures("account", key, WINDOW) >= ACCOUNT_LIMIT
            or users.failures("ip", _ip(request), WINDOW) >= IP_LIMIT)


def _fail(users: UserStore, request: Request, key: str) -> None:
    users.record_failure("account", key)
    users.record_failure("ip", _ip(request))


def landing_page(rec: UserRecord, settings) -> str:
    return forced_page(rec, settings) or "/"


def complete_sign_in(request: Request, rec: UserRecord, method: str = "local") -> RedirectResponse:
    """Replace the session with a signed-in one and say where to go next."""
    state = request.app.state
    state.users.touch_sign_in(rec.id)
    state.users.clear_failures("account", _key(rec.email))
    request.session.clear()  # the user_id change rotates the cookie
    request.session["user_id"] = rec.id
    request.session["csrf"] = secrets.token_urlsafe(32)
    state.store.add_audit(rec.email, "signin",
                          "Signed in (Microsoft)" if method == "entra" else "Signed in (local)")
    return RedirectResponse(landing_page(rec, state.settings), status_code=303)


def setup_signin(app: FastAPI) -> None:
    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        response = app.state.templates.TemplateResponse(
            request, name, {"csrf": csrf_token(request), **ctx}, status_code=status)
        response.headers.update(NO_STORE)
        return response

    def signin_page(request: Request, status: int = 200, error: str | None = None):
        s = app.state.settings
        entra = bool(s.entra_enabled)
        return page(request, "signin.html", status, error=error, entra=entra,
                    local=bool(s.local_sign_in),
                    disclosure=bool(s.hide_local_for_non_admins and entra))

    def totp_page(request: Request, status: int = 200, error: str | None = None):
        return page(request, "totp.html", status, error=error)

    @app.get("/auth/login", include_in_schema=False)
    async def login_form(request: Request):
        return signin_page(request)

    @app.post("/auth/login", include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def login(request: Request, email: str = Form(""), password: str = Form("")):
        users: UserStore = app.state.users
        if not app.state.settings.local_sign_in:
            return signin_page(request, 403, "Local sign-in is turned off")
        key = _key(email)
        # Checked before the password, so a locked key says the same for every email.
        if _locked(users, request, key):
            return signin_page(request, 429, LOCKED_MESSAGE)
        rec = users.by_email("local", email) if key else None
        stored = rec.password_hash if rec is not None and rec.password_hash else DUMMY_HASH
        ok = verify_password(stored, password)  # always hashes: equal work either way
        if rec is None or rec.disabled or not rec.password_hash or not ok:
            _fail(users, request, key)
            app.state.store.add_audit(email.strip()[:200] or "unknown", "signin",
                                      f"Sign-in failed: {email.strip()[:200]}")
            if _locked(users, request, key):
                app.state.store.add_audit(email.strip()[:200] or "unknown", "signin",
                                          f"Sign-in locked: {email.strip()[:200]}")
            return signin_page(request, 401, BAD_CREDENTIALS)
        if needs_rehash(rec.password_hash):
            users.set_password_hash(rec.id, hash_password(password), rec.must_change_password)
        if rec.totp_enabled:
            request.session["pending_user_id"] = rec.id
            request.session["pending_at"] = users.now().timestamp()
            return RedirectResponse("/auth/totp", status_code=303)
        return complete_sign_in(request, rec)

    def pending_user(request: Request) -> UserRecord | None:
        users: UserStore = app.state.users
        uid = request.session.get("pending_user_id")
        at = request.session.get("pending_at")
        valid = (isinstance(uid, int) and not isinstance(uid, bool)
                 and isinstance(at, (int, float))
                 and 0 <= users.now().timestamp() - at <= PENDING_TTL.total_seconds())
        rec = users.get(uid) if valid else None
        if rec is None or rec.disabled or not rec.totp_enabled:
            request.session.pop("pending_user_id", None)
            request.session.pop("pending_at", None)
            return None
        return rec

    @app.get("/auth/totp", include_in_schema=False)
    async def totp_form(request: Request):
        if pending_user(request) is None:
            return RedirectResponse("/auth/login", status_code=303)
        return totp_page(request)

    @app.post("/auth/totp", include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def totp_submit(request: Request, code: str = Form("")):
        users: UserStore = app.state.users
        rec = pending_user(request)
        if rec is None:
            return RedirectResponse("/auth/login", status_code=303)
        key = _key(rec.email)
        if _locked(users, request, key):
            return totp_page(request, 429, LOCKED_MESSAGE)
        cleaned = code.replace(" ", "").strip()
        if len(cleaned) == 6 and cleaned.isascii() and cleaned.isdigit():
            secret = users.totp_secret(rec)
            step = totp.verify_code(secret, cleaned, rec.totp_last_step) if secret else None
            if step is not None:
                users.set_totp_last_step(rec.id, step)
                return complete_sign_in(request, rec)
        elif cleaned and users.use_recovery_code(rec.id, totp.hash_recovery_code(cleaned)):
            app.state.store.add_audit(rec.email, "signin", "Recovery code used")
            return complete_sign_in(request, rec)
        _fail(users, request, key)
        app.state.store.add_audit(rec.email, "signin", f"Two-step code refused: {rec.email}")
        return totp_page(request, 401, BAD_CODE)

    # --- signed-in account pages -------------------------------------------

    def account_page(request: Request, name: str, user: User, status: int = 200, **ctx):
        return page(request, name, status, user=user, **ctx)

    def local_record(user: User) -> UserRecord | None:
        rec = app.state.users.get(user.id)
        return rec if rec is not None and rec.kind == "local" else None

    def password_page(request, user, rec, status=200, errors=None, done=False):
        return account_page(request, "password_change.html", user, status,
                            forced=rec.must_change_password, errors=errors or {}, done=done)

    @app.get(PASSWORD_PAGE, include_in_schema=False, response_class=HTMLResponse)
    async def password_form(request: Request, user: User = Depends(current_user)):
        rec = local_record(user)
        if rec is None:
            return RedirectResponse("/", status_code=303)
        return password_page(request, user, rec)

    @app.post(PASSWORD_PAGE, include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def password_submit(
        request: Request, user: User = Depends(current_user),
        current_password: str = Form(""), new_password: str = Form(""),
        confirm_password: str = Form(""),
    ):
        users: UserStore = app.state.users
        rec = local_record(user)
        if rec is None:
            return RedirectResponse("/", status_code=303)
        key = _key(rec.email)
        errors: dict[str, str] = {}
        if not rec.must_change_password:
            if _locked(users, request, key):
                errors["current_password"] = LOCKED_MESSAGE
            elif not verify_password(rec.password_hash or "", current_password):
                _fail(users, request, key)
                errors["current_password"] = "That isn't your current password"
        problem = password_problem(new_password)
        if problem:
            errors["new_password"] = problem
        elif new_password != confirm_password:
            errors["confirm_password"] = "The two passwords don't match"
        elif rec.password_hash and verify_password(rec.password_hash, new_password):
            errors["new_password"] = "Choose a password you haven't used here before"
        if errors:
            return password_page(request, user, rec, 400, errors)
        users.set_password_hash(rec.id, hash_password(new_password), False)
        cookie = request.cookies.get(COOKIE)
        users.end_sessions(rec.id, SessionStore.token_hash(cookie) if cookie else None)
        app.state.store.add_audit(rec.email, "account", "Password changed")
        fresh = users.get(rec.id)
        nxt = forced_page(fresh, app.state.settings)
        if nxt or rec.must_change_password:
            return RedirectResponse(nxt or "/", status_code=303)
        return password_page(request, user, fresh, done=True)

    def two_step_setup(request: Request, user: User, rec: UserRecord, status=200, error=None):
        secret = request.session.get("totp_setup_secret")
        if not isinstance(secret, str):
            secret = totp.new_secret()
            request.session["totp_setup_secret"] = secret
        uri = totp.provisioning_uri(secret, rec.email, app.state.settings.server_name)
        return account_page(request, "totp_setup.html", user, status, stage="setup",
                            qr=totp.qr_svg(uri), secret=secret, error=error,
                            forced=forced_page(rec, app.state.settings) == TWO_STEP_PAGE)

    @app.get(TWO_STEP_PAGE, include_in_schema=False, response_class=HTMLResponse)
    async def two_step_form(request: Request, user: User = Depends(current_user)):
        rec = local_record(user)
        if rec is None:
            return RedirectResponse("/", status_code=303)
        if rec.totp_enabled:
            return account_page(request, "totp_setup.html", user, stage="on")
        return two_step_setup(request, user, rec)

    @app.post(TWO_STEP_PAGE, include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def two_step_submit(request: Request, user: User = Depends(current_user),
                              code: str = Form("")):
        users: UserStore = app.state.users
        rec = local_record(user)
        if rec is None:
            return RedirectResponse("/", status_code=303)
        if rec.totp_enabled:
            return RedirectResponse(TWO_STEP_PAGE, status_code=303)
        secret = request.session.get("totp_setup_secret")
        step = totp.verify_code(secret, code, None) if isinstance(secret, str) else None
        if step is None:
            return two_step_setup(request, user, rec, 400, BAD_CODE)
        users.set_totp(rec.id, secret, True)
        users.set_totp_last_step(rec.id, step)
        codes = totp.new_recovery_codes()
        users.set_recovery_codes(rec.id, [totp.hash_recovery_code(c) for c in codes])
        request.session.pop("totp_setup_secret", None)
        app.state.store.add_audit(rec.email, "account", "Two-step sign-in turned on")
        return account_page(request, "totp_setup.html", user, stage="codes", codes=codes)
