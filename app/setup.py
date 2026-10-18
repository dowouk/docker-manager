"""First start: the setup token, the first Admin, and the one-off upgrade of 0.1/0.2 access rules.

Setup is open only on a brand-new install with no users. An upgraded database is never
"open" (the migration in ``prepare`` keeps its owners in), and once the first Admin exists
the token file is deleted and ``/setup`` is closed for good.
"""
import asyncio
import logging
import os
import re
import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import csrf_token, verify_csrf
from app.passwords import hash_password, password_problem
from app.sessions import _COOKIE_ATTRS, _COOKIE_MAX_AGE, COOKIE
from app.settings_service import _GUID, SettingsService, _valid_public_url
from app.store import Store
from app.users import DuplicateUserError, UserRecord, UserStore

log = logging.getLogger(__name__)

TOKEN_FILE = "setup-token"
IP_LIMIT = 20
WINDOW = timedelta(minutes=15)
CLOSED_MESSAGE = "Setup has already been completed – sign in"
_SYSTEM = "system"
MAX_PASSWORD_FILE = 4096
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")


def _valid_email(value: str) -> bool:
    local, at, domain = value.partition("@")
    return bool(at and local and domain and "@" not in domain and not any(c.isspace() for c in value))


class Setup:
    def __init__(self, store: Store, users: UserStore, settings_service: SettingsService,
                 data_dir: str, clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._users = users
        self._settings_service = settings_service
        self._token_path = os.path.join(data_dir, TOKEN_FILE)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._closed = False
        self._lock = threading.Lock()
        self.on_close: list[Callable[[], None]] = []

    # --- state -------------------------------------------------------------

    def is_open(self) -> bool:
        """No users and not an upgraded install. Once False it stays False."""
        if self._closed:
            return False
        if self._store.get_meta("existing_install") == "1" or self._users.count() > 0:
            self._close()
            return False
        return True

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._remove_token()  # however setup closed, the token is useless now
        for callback in self.on_close:
            callback()

    # --- startup -----------------------------------------------------------

    def prepare(self, settings) -> None:
        """Run once at startup, before any request is served."""
        if (self._store.get_meta("existing_install") == "1"
                and self._store.get_meta("allow_rules_migrated") is None):
            self.migrate_allow_rules(settings)
        elif (self._store.get_meta("existing_install") != "1"
              and self._users.count() == 0):
            if not self._initial_admin(settings):
                self._announce_token(settings)

    def migrate_allow_rules(self, settings) -> None:
        """Turn the 0.1/0.2 allow rules into accounts and mappings, once."""
        done: list[str] = []
        emails = [e.strip() for e in (settings.allowed_emails or []) if e and e.strip()]
        group = (settings.allowed_group_id or "").strip()
        for upn in emails:
            try:
                self._users.create("entra", upn, upn, "admin", pinned=True)
                done.append(f"{upn} is an Admin")
            except DuplicateUserError:
                pass
        if group and emails:
            # 0.2 required BOTH (email AND group); mapping the group as well would let
            # every group member in, so only the listed people carry over.
            log.error("Both ALLOWED_EMAILS and ALLOWED_GROUP_ID were set. 0.2 needed both, "
                      "so only the listed people were kept as Admins and the group was not "
                      "mapped; add a group role on the Settings page if you want it")
            done.append("the old allowed group was not mapped because it was combined with "
                        "the email list – add a group role on the Settings page if wanted")
        elif group:
            service = self._settings_service
            skipped = None
            if not _GUID.fullmatch(group):
                skipped = "isn't a group Object ID (a GUID)"
            else:
                try:
                    service.save_group_mappings(
                        service.group_mappings() + [{"group_id": group, "role": "admin"}],
                        _SYSTEM, f"Group {group} mapped to Admin (from the old allowed group)")
                    done.append(f"group {group} maps to Admin")
                except ValueError:
                    skipped = "couldn't be saved as a group role"
            if skipped:
                log.error("ALLOWED_GROUP_ID %s, so it was not migrated; add a group role "
                          "on the Settings page%s", skipped,
                          " (or run `docker-manager reset-admin <email>` to get back in)")
                done.append("the old allowed group was skipped – it " + skipped)
        if emails or group:
            # Never open the doors: people outside the old rules wait for approval.
            self._store.save_settings({"default_role": "pending"})
            done.append("new Microsoft sign-ins wait for approval")
        else:
            self._store.save_settings({"default_role": "admin_legacy"})
            done.append("everyone who signs in with Microsoft is an Admin, as in 0.1")
        self._store.set_meta("allow_rules_migrated", "1")
        self._store.add_audit(_SYSTEM, "users",
                              "Upgraded from an earlier version: " + "; ".join(done))

    def _initial_admin(self, settings) -> bool:
        email = os.environ.get("INITIAL_ADMIN_EMAIL", "").strip()
        path = os.environ.get("INITIAL_ADMIN_PASSWORD_FILE", "").strip()
        if not email or not path:
            return False
        if not settings.public_url:
            log.error("INITIAL_ADMIN_EMAIL needs PUBLIC_URL as well; "
                      "falling back to the setup token")
            return False
        if not _valid_email(email):
            log.error("INITIAL_ADMIN_EMAIL isn't a valid email address; "
                      "falling back to the setup token")
            return False
        try:
            with open(path, encoding="utf-8") as f:
                password = f.read(MAX_PASSWORD_FILE + 1)
        except (OSError, UnicodeDecodeError) as exc:
            log.error("Can't read INITIAL_ADMIN_PASSWORD_FILE (%s); "
                      "falling back to the setup token", type(exc).__name__)
            return False
        if len(password) > MAX_PASSWORD_FILE:
            log.error("INITIAL_ADMIN_PASSWORD_FILE is larger than %d bytes; "
                      "falling back to the setup token", MAX_PASSWORD_FILE)
            return False
        password = password.rstrip("\r\n")
        problem = password_problem(password)
        if problem:
            log.error("The initial admin password is refused (%s); "
                      "falling back to the setup token", problem)
            return False
        try:
            rec = self._users.create_first_admin(
                "local", email, email.split("@")[0], "admin", pinned=True,
                password_hash=hash_password(password), must_change_password=True)
        except ValueError:
            log.error("The initial admin password has invalid characters; "
                      "falling back to the setup token")
            return False
        if rec is None:
            return True  # someone got there first
        self._finish(rec)
        log.info("Created the first Admin %s from INITIAL_ADMIN_EMAIL", email)
        return True

    def _announce_token(self, settings) -> None:
        token = self._ensure_token()
        where = (settings.public_url or "").rstrip("/") or "the dashboard"
        # The one place the token is logged (the spec's way of getting it to the owner).
        log.info("Setup: open %s/setup and enter the token from %s (also shown here: %s)",
                 where, self._token_path, token)

    def _ensure_token(self) -> str:
        existing = self._read_token()
        if existing and _TOKEN_SHAPE.fullmatch(existing):
            try:
                os.chmod(self._token_path, 0o600)
            except OSError:
                pass
            return existing
        token = secrets.token_urlsafe(32)
        os.makedirs(os.path.dirname(self._token_path) or ".", exist_ok=True)
        try:
            os.unlink(self._token_path)  # an empty or unreadable leftover
        except FileNotFoundError:
            pass
        fd = os.open(self._token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        return token

    def _read_token(self) -> str | None:
        try:
            with open(self._token_path, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None

    # --- the token ---------------------------------------------------------

    def locked(self, ip: str) -> bool:
        return self._users.failures("setup", ip, WINDOW) >= IP_LIMIT

    def just_blocked(self, ip: str) -> bool:
        """True only for the failure that tipped this address into the block."""
        return self._users.failures("setup", ip, WINDOW) == IP_LIMIT

    def check_token(self, token: str, ip: str) -> bool:
        """Constant-time compare; a failure counts against the address."""
        if not self.is_open() or self.locked(ip):
            return False
        expected = self._read_token()
        ok = (expected is not None and isinstance(token, str)
              and secrets.compare_digest(token.strip().encode(), expected.encode()))
        if not ok:
            self._users.record_failure("setup", ip)
        return ok

    # --- completing --------------------------------------------------------

    def complete_local(self, name: str, email: str, password: str,
                       public_url: str | None = None) -> UserRecord | None:
        email = email.strip()
        name = name.strip() or email
        if not _valid_email(email):
            raise ValueError("Enter a valid email address")
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        if not self.is_open():
            return None
        rec = self._users.create_first_admin(
            "local", email, name, "admin", pinned=True, password_hash=hash_password(password))
        if rec is None:
            self._close()
            return None
        self._finish(rec, public_url)
        return rec

    def complete_with_entra(self, identity) -> UserRecord | None:
        """The first Admin signed in with Microsoft. Re-checks that setup is still open;
        the unique first-user insert decides a race."""
        if not self.is_open():
            return None
        rec = self._users.create_first_admin(
            "entra", identity.upn, identity.name, "admin", pinned=True,
            entra_tid=identity.tid, entra_oid=identity.oid)
        if rec is None:
            self._close()
            return None
        self._finish(rec)
        return rec

    def _remove_token(self) -> None:
        try:
            os.unlink(self._token_path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Couldn't delete the setup token file %s; delete it by hand",
                        self._token_path)

    def _finish(self, rec: UserRecord, public_url: str | None = None) -> None:
        self._store.set_meta("setup_complete", "1")
        if public_url and not self._settings_service.effective().public_url \
                and _valid_public_url(public_url):
            self._store.save_settings({"public_url": public_url})
        self._store.add_audit(rec.email, "users", f"First Admin created: {rec.email}")
        self._close()


def _request_origin(request: Request) -> str | None:
    """scheme + host of the setup request. Only the token holder reaches here, which is
    why (and only why) the request's own headers are good enough.

    This is the deliberate, setup-only exception to "never derive public_url from the
    request": nobody else can reach it, and an Admin can change the address afterwards."""
    parts = urlsplit(f"{request.url.scheme}://{request.url.netloc}")
    origin = f"{parts.scheme}://{parts.netloc}"
    return origin if _valid_public_url(origin) else None


def setup_routes(app: FastAPI) -> None:
    def setup() -> Setup:
        return app.state.setup

    def page(request: Request, stage: str, status: int = 200, **ctx) -> HTMLResponse:
        response = app.state.templates.TemplateResponse(
            request, "setup.html", {"csrf": csrf_token(request), "stage": stage, **ctx},
            status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    def closed(request: Request) -> HTMLResponse:
        return page(request, "closed", 404)

    def ip_of(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @app.get("/setup", include_in_schema=False)
    async def token_form(request: Request):
        if not setup().is_open():
            return closed(request)
        return page(request, "token")

    @app.post("/setup/token", include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def token_submit(request: Request, token: str = Form("")):
        if not setup().is_open():
            return closed(request)
        ip = ip_of(request)
        if setup().locked(ip):
            return page(request, "token", 429,
                        error="Too many attempts – try again in 15 minutes.")
        if not setup().check_token(token, ip):
            if setup().just_blocked(ip):  # only the block is audited, not every guess
                app.state.store.add_audit(ip, "setup", "Setup token refused: address blocked")
            return page(request, "token", 401, error="That token isn't right")
        # Fresh session and CSRF token from here on (the cookie changes with it).
        sessions = app.state.sessions
        old = request.cookies.get(COOKIE)
        if old:
            sessions.delete(old)
        fresh = sessions.create({"setup_ok": True, "csrf": secrets.token_urlsafe(32)})
        response = RedirectResponse("/setup/admin", status_code=303)
        response.headers.append(
            "set-cookie", f"{COOKIE}={fresh}; Max-Age={_COOKIE_MAX_AGE}; {_COOKIE_ATTRS}")
        return response

    def admin_page(request: Request, status: int = 200, **ctx):
        return page(request, "admin", status, entra=bool(app.state.settings.entra_enabled),
                    **ctx)

    @app.get("/setup/admin", include_in_schema=False)
    async def admin_form(request: Request):
        if not setup().is_open():
            return closed(request)
        if not request.session.get("setup_ok"):
            return RedirectResponse("/setup", status_code=303)
        return admin_page(request)

    @app.post("/setup/admin", include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def admin_submit(request: Request, name: str = Form(""), email: str = Form(""),
                           password: str = Form(""), confirm_password: str = Form("")):
        from app.signin import complete_sign_in  # deferred: keeps imports one-way

        if not setup().is_open():
            return closed(request)
        if not request.session.get("setup_ok"):
            return RedirectResponse("/setup", status_code=303)
        errors: dict[str, str] = {}
        if not _valid_email(email.strip()):
            errors["email"] = "Enter a valid email address"
        problem = password_problem(password)
        if problem:
            errors["password"] = problem
        elif password != confirm_password:
            errors["confirm_password"] = "The two passwords don't match"
        if errors:
            return admin_page(request, 400, errors=errors, form={"name": name, "email": email})
        try:
            rec = await asyncio.to_thread(
                setup().complete_local, name, email, password, _request_origin(request))
        except ValueError as exc:
            return admin_page(request, 400, errors={"password": str(exc)},
                              form={"name": name, "email": email})
        if rec is None:
            request.session.clear()
            return page(request, "lost", 409, message=CLOSED_MESSAGE)
        from app.main import apply_settings  # deferred: main imports this module

        apply_settings(app)
        return complete_sign_in(request, rec, "local")

    @app.post("/setup/entra", include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def entra_start(request: Request):
        if not setup().is_open():
            return closed(request)
        if not request.session.get("setup_ok"):
            return RedirectResponse("/setup", status_code=303)
        if not app.state.settings.entra_enabled:
            raise HTTPException(status_code=404)
        request.session["setup_entra"] = True
        return RedirectResponse("/auth/entra", status_code=303)
