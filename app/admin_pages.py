"""Users page, Account page and invites.

One-time passwords, invite links and recovery codes are rendered straight in the POST
response (never in a redirect URL, the audit log or the database in plain text).
"""
import asyncio
import secrets

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import totp
from app.auth import (
    SIGNED_OUT_URL,
    User, csrf_token, current_user, require_role, verify_csrf,
)
from app.passwords import hash_password, password_problem, verify_password
from app.roles import ROLE_LABELS, Role
from app.setup import _valid_email
from app.signin import LOCKED_MESSAGE, _audit_trips, _fail, _key, _locked, complete_sign_in
from app.users import DuplicateUserError, LastAdminError, UserRecord, UserStore

LAST_ADMIN = "There must always be at least one active Admin"
ASSIGNABLE = ("viewer", "operator", "admin")
NO_STORE = {"Cache-Control": "no-store"}
# No look-alikes (0/O, 1/l/I) so a password read off the screen can be typed back.
_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_LENGTH = 16

FLASH = {
    "created": "User added",
    "role": "Role saved",
    "unpinned": "Entra will decide this user's role at their next sign-in",
    "disabled": "User disabled and signed out",
    "enabled": "User enabled",
    "two_step_reset": "Two-step sign-in reset – they'll set it up again at next sign-in",
    "signed_out": "User signed out everywhere",
    "deleted": "User deleted",
}


def one_time_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def setup_admin_pages(app: FastAPI) -> None:
    admin = require_role(Role.ADMIN)

    def page(request: Request, template: str, status: int = 200, *, user: User | None = None,
             public: bool = False, **ctx) -> HTMLResponse:
        response = app.state.templates.TemplateResponse(
            request, template, {"user": user, "csrf": csrf_token(request), **ctx},
            status_code=status)
        response.headers.update(NO_STORE)
        if public:
            response.headers["Referrer-Policy"] = "no-referrer"  # the URL carries a token
        return response

    def audit(user: User, summary: str) -> None:
        app.state.store.add_audit(user.email, "users", summary)

    def target(user_id: int) -> UserRecord:
        rec = app.state.users.get(user_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="No such user")
        return rec

    def users_page(request: Request, user: User, status: int = 200, error: str | None = None,
                   msg: str = "", result: dict | None = None):
        rows = app.state.users.list()
        # Waiting for approval first, then the store's own order (admins, then by name).
        rows.sort(key=lambda r: r.role != "pending")
        return page(request, "users.html", status, user=user, rows=rows, error=error,
                    flash=FLASH.get(msg), result=result, assignable=ASSIGNABLE,
                    role_labels={r.value: ROLE_LABELS[r] for r in Role},
                    public_url=bool(app.state.settings.public_url),
                    local_on=app.state.settings.local_sign_in,
                    entra_on=app.state.settings.entra_enabled)

    def done(msg: str) -> RedirectResponse:
        return RedirectResponse(f"/users?msg={msg}", status_code=303)

    def role_or_400(role: str) -> str | None:
        return role if role in ASSIGNABLE else None

    @app.get("/users", include_in_schema=False, response_class=HTMLResponse)
    async def users_list(request: Request, msg: str = "", user: User = Depends(admin)):
        return users_page(request, user, msg=msg)

    @app.get("/users/{user_id}", include_in_schema=False, response_class=HTMLResponse)
    async def user_detail(request: Request, user_id: int, user: User = Depends(admin)):
        rec = target(user_id)
        return page(request, "user.html", 200, user=user, r=rec, assignable=ASSIGNABLE,
                    role_labels={r.value: ROLE_LABELS[r] for r in Role},
                    local_on=app.state.settings.local_sign_in)

    @app.post("/users/local", include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def users_add_local(request: Request, user: User = Depends(admin),
                              name: str = Form(""), email: str = Form(""),
                              role: str = Form(""), method: str = Form("")):
        users: UserStore = app.state.users
        email = email.strip()
        name = name.strip() or email
        if not app.state.settings.local_sign_in:
            return users_page(request, user, 400,
                              "Local sign-in is turned off – turn it on in Settings first")
        if role_or_400(role) is None or method not in ("password", "invite"):
            return users_page(request, user, 400, "Choose a role and how they get started")
        if not _valid_email(email):
            return users_page(request, user, 400, "Enter a valid email address")
        password = one_time_password() if method == "password" else None
        try:
            rec = users.create(
                "local", email, name[:200], role,
                pinned=True,
                password_hash=(await asyncio.to_thread(hash_password, password)
                               if password else None),
                must_change_password=bool(password))
        except DuplicateUserError:
            return users_page(request, user, 400, "A user with that email already exists")
        audit(user, f"Added local user {rec.email} as {ROLE_LABELS[Role(role)]}"
                    f" ({'one-time password' if password else 'invite link'})")
        if password:
            return users_page(request, user, result={
                "kind": "password", "email": rec.email, "password": password})
        return invite_result(request, user, rec)

    def invite_result(request: Request, user: User, rec: UserRecord):
        path = f"/auth/invite/{app.state.users.create_invite(rec.id)}"
        public = (app.state.settings.public_url or "").rstrip("/")
        return users_page(request, user, result={
            "kind": "invite", "email": rec.email, "path": path,
            "url": public + path if public else None})

    @app.post("/users/{user_id}/invite", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_invite(request: Request, user_id: int, user: User = Depends(admin)):
        rec = target(user_id)
        if rec.kind != "local":
            return users_page(request, user, 400, "Microsoft users sign in with Microsoft")
        if not app.state.settings.local_sign_in:
            return users_page(request, user, 400,
                              "Local sign-in is turned off – turn it on in Settings first")
        if not rec.active:
            return users_page(request, user, 400, "Turn this account on before inviting them")
        audit(user, f"Created a new invite link for {rec.email}")
        return invite_result(request, user, rec)

    @app.post("/users/entra", include_in_schema=False, dependencies=[Depends(verify_csrf)])
    async def users_add_entra(request: Request, user: User = Depends(admin),
                              upn: str = Form(""), role: str = Form("")):
        upn = upn.strip()
        if role_or_400(role) is None:
            return users_page(request, user, 400, "Choose a role")
        if not _valid_email(upn):
            return users_page(request, user, 400, "Enter their Microsoft sign-in name (UPN)")
        try:
            app.state.users.create("entra", upn, upn, role, pinned=True)
        except DuplicateUserError:
            return users_page(request, user, 400, "A user with that name already exists")
        audit(user, f"Added Microsoft user {upn} as {ROLE_LABELS[Role(role)]}")
        return done("created")

    def guarded(request, user, fn):
        """Run a store change, turning the last-admin refusal into a 400 page."""
        try:
            return fn()
        except LastAdminError:
            return users_page(request, user, 400, LAST_ADMIN)

    @app.post("/users/{user_id}/role", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_role(request: Request, user_id: int, user: User = Depends(admin),
                         role: str = Form("")):
        rec = target(user_id)
        if role_or_400(role) is None:
            return users_page(request, user, 400, "Choose a role")

        def change():
            app.state.users.update(rec.id, role=role, role_pinned=True)
            label = ROLE_LABELS[Role(role)]
            audit(user, f"Approved {rec.email} as {label}" if rec.role == "pending"
                  else f"Set {rec.email} to {label} (pinned)")
            return done("role")
        return guarded(request, user, change)

    @app.post("/users/{user_id}/unpin", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_unpin(request: Request, user_id: int, user: User = Depends(admin)):
        rec = target(user_id)
        if rec.kind != "entra":
            return users_page(request, user, 400, "Only Microsoft users can follow Entra roles")
        app.state.users.update(rec.id, role_pinned=False)
        audit(user, f"Let Entra decide the role of {rec.email}")
        return done("unpinned")

    @app.post("/users/{user_id}/disable", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_disable(request: Request, user_id: int, user: User = Depends(admin)):
        rec = target(user_id)

        def change():
            app.state.users.update(rec.id, disabled=True)
            app.state.users.end_sessions(rec.id)
            app.state.users.delete_invites(rec.id)
            audit(user, f"Disabled {rec.email}")
            return done("disabled")
        return guarded(request, user, change)

    @app.post("/users/{user_id}/enable", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_enable(user_id: int, user: User = Depends(admin)):
        rec = target(user_id)
        app.state.users.update(rec.id, disabled=False)
        audit(user, f"Enabled {rec.email}")
        return done("enabled")

    @app.post("/users/{user_id}/reset-password", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_reset_password(request: Request, user_id: int,
                                   user: User = Depends(admin)):
        rec = target(user_id)
        if rec.kind != "local":
            return users_page(request, user, 400, "Microsoft users sign in with Microsoft")
        password = one_time_password()
        app.state.users.set_password_hash(
            rec.id, await asyncio.to_thread(hash_password, password), True)
        app.state.users.end_sessions(rec.id)
        app.state.users.delete_invites(rec.id)
        audit(user, f"Reset the password of {rec.email} (one-time password)")
        return users_page(request, user, result={
            "kind": "password", "email": rec.email, "password": password})

    @app.post("/users/{user_id}/reset-two-step", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_reset_two_step(request: Request, user_id: int,
                                   user: User = Depends(admin)):
        rec = target(user_id)
        if rec.kind != "local":
            return users_page(request, user, 400, "Two-step sign-in is managed by Microsoft")
        app.state.users.clear_two_step(rec.id)
        app.state.users.end_sessions(rec.id)
        audit(user, f"Reset two-step sign-in for {rec.email}")
        return done("two_step_reset")

    @app.post("/users/{user_id}/sign-out", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_sign_out(user_id: int, user: User = Depends(admin)):
        rec = target(user_id)
        app.state.users.end_sessions(rec.id)
        audit(user, f"Signed {rec.email} out everywhere")
        return done("signed_out")

    @app.post("/users/{user_id}/delete", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def users_delete(request: Request, user_id: int, user: User = Depends(admin),
                           confirm_email: str = Form("")):
        rec = target(user_id)
        if confirm_email.strip().lower() != rec.email.lower():
            return users_page(request, user, 400,
                              "That isn't this user's email address – nothing was deleted")

        def change():
            app.state.users.delete(rec.id)
            audit(user, f"Deleted {rec.email}")
            return done("deleted")
        return guarded(request, user, change)

    # --- invites (public) --------------------------------------------------

    def invite_gate(request: Request) -> RedirectResponse | None:
        # /auth/ is public, so keep invites out of an unfinished install ourselves.
        if app.state.setup.is_open():
            return RedirectResponse("/setup", status_code=303)
        if not app.state.settings.local_sign_in:
            return page(request, "invite.html", 403, public=True, usable=False, off=True)
        return None

    def invite_page(request: Request, token: str, rec: UserRecord | None, status: int = 200,
                    errors: dict | None = None, name: str | None = None):
        usable = rec is not None and rec.active and rec.kind == "local"
        return page(request, "invite.html", status if usable else 410, public=True,
                    usable=usable, token=token, errors=errors or {},
                    email=rec.email if usable else "", name=name if name is not None
                    else (rec.name if usable else ""))

    @app.get("/auth/invite/{token}", include_in_schema=False, response_class=HTMLResponse)
    async def invite_form(request: Request, token: str):
        if (gate := invite_gate(request)) is not None:
            return gate
        return invite_page(request, token, app.state.users.peek_invite(token))

    @app.post("/auth/invite/{token}", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def invite_submit(request: Request, token: str, name: str = Form(""),
                            new_password: str = Form(""), confirm_password: str = Form("")):
        if (gate := invite_gate(request)) is not None:
            return gate
        users: UserStore = app.state.users
        rec = users.peek_invite(token)
        if rec is None or not rec.active or rec.kind != "local":
            return invite_page(request, token, rec)
        errors: dict[str, str] = {}
        problem = password_problem(new_password)
        if problem:
            errors["new_password"] = problem
        elif new_password != confirm_password:
            errors["confirm_password"] = "The two passwords don't match"
        if errors:  # the invite stays usable: a typo must not burn it
            return invite_page(request, token, rec, 400, errors, name)
        hashed = await asyncio.to_thread(hash_password, new_password)
        used = users.use_invite(token)  # atomic: only one submission wins
        if used is None or not used.active or used.kind != "local":
            return invite_page(request, token, None)
        clean = name.strip()[:200]
        if clean:
            users.update(used.id, name=clean)
        users.set_password_hash(used.id, hashed, False)
        # A new password means no earlier session can be trusted (an invite can be
        # re-issued for an account that was already in use).
        users.end_sessions(used.id)
        app.state.store.add_audit(used.email, "users", "Accepted invite and set a password")
        fresh = users.get(used.id)
        if fresh.totp_enabled:
            # The invite proves the link, not the second factor: same step as a password sign-in.
            request.session["pending_user_id"] = fresh.id
            request.session["pending_at"] = users.now().timestamp()
            return RedirectResponse("/auth/totp", status_code=303)
        return complete_sign_in(request, fresh)

    # --- account -----------------------------------------------------------

    def account_page(request: Request, user: User, status: int = 200, **ctx):
        rec = app.state.users.get(user.id)
        return page(request, "account.html", status, user=user, rec=rec,
                    role_label=ROLE_LABELS[user.role],
                    two_step_required=bool(
                        rec and rec.kind == "local" and rec.role == "admin"
                        and app.state.settings.require_totp_admins),
                    **ctx)

    @app.get("/account", include_in_schema=False, response_class=HTMLResponse)
    async def account(request: Request, user: User = Depends(current_user)):
        return account_page(request, user)

    async def password_ok(request: Request, rec: UserRecord, password: str) -> str | None:
        """None if the current password is right, otherwise the message to show."""
        users: UserStore = app.state.users
        key = _key(rec.email)
        if _locked(users, request, key):
            return LOCKED_MESSAGE
        if not await asyncio.to_thread(verify_password, rec.password_hash or "", password):
            _audit_trips(request, rec.email, _fail(users, request, key))
            return "That isn't your current password"
        return None

    def own_local(user: User) -> UserRecord:
        rec = app.state.users.get(user.id)
        if rec is None or rec.kind != "local":
            raise HTTPException(status_code=404, detail="Not available for Microsoft accounts")
        return rec

    @app.post("/account/two-step/off", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def two_step_off(request: Request, user: User = Depends(current_user),
                           current_password: str = Form("")):
        rec = own_local(user)
        if rec.role == "admin" and app.state.settings.require_totp_admins:
            return account_page(request, user, 400,
                                error="Admins must keep two-step sign-in turned on")
        problem = await password_ok(request, rec, current_password)
        if problem:
            return account_page(request, user, 400, error=problem)
        app.state.users.clear_two_step(rec.id)
        app.state.store.add_audit(rec.email, "account", "Two-step sign-in turned off")
        return account_page(request, user, notice="Two-step sign-in is turned off")

    @app.post("/account/recovery-codes", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def recovery_codes(request: Request, user: User = Depends(current_user),
                             current_password: str = Form("")):
        rec = own_local(user)
        if not rec.totp_enabled:
            return account_page(request, user, 400, error="Turn on two-step sign-in first")
        problem = await password_ok(request, rec, current_password)
        if problem:
            return account_page(request, user, 400, error=problem)
        codes = totp.new_recovery_codes()
        app.state.users.set_recovery_codes(rec.id, [totp.hash_recovery_code(c) for c in codes])
        app.state.store.add_audit(rec.email, "account", "New recovery codes created")
        return account_page(request, user, codes=codes)

    @app.post("/account/sign-out-everywhere", include_in_schema=False,
              dependencies=[Depends(verify_csrf)])
    async def sign_out_everywhere(request: Request, user: User = Depends(current_user)):
        app.state.users.end_sessions(user.id)
        app.state.store.add_audit(user.email, "account", "Signed out everywhere")
        request.session.clear()
        return RedirectResponse(SIGNED_OUT_URL, status_code=303)
