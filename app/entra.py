"""Microsoft Entra ID sign-in: OIDC code flow with PKCE, token checks, and matching a
verified identity to an account with a role.

The OAuth client is built from the *effective* settings and rebuilt whenever settings
are saved, so values entered on the Settings page work without a restart. The redirect
URI always comes from the configured public URL, never from the request.
"""
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from authlib.common.errors import AuthlibBaseError
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, Request
from joserfc.errors import JoseError
from starlette.responses import RedirectResponse

from app.auth import _page
from app.config import Settings
from app.roles import Role, default_role_for_new, mapped_role
from app.signin import complete_sign_in
from app.users import DuplicateUserError, LastAdminError, UserRecord, UserStore

log = logging.getLogger(__name__)

NOT_SET_UP = "Microsoft sign-in isn't set up"


@dataclass(frozen=True)
class EntraIdentity:
    """Who a verified Entra token says signed in (not yet an account)."""
    tid: str
    oid: str
    upn: str
    name: str
    roles: list[str]
    groups: list[str] | None  # None: the token carried no groups claim (e.g. overage)
    matchable: bool = True  # upn is preferred_username; the email claim may be unverified


def issuer(tenant_id: str | None) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


def metadata_url(tenant_id: str | None) -> str:
    return f"https://login.microsoftonline.com/{quote(tenant_id or '', safe='')}/v2.0/.well-known/openid-configuration"


def redirect_uri(settings: Settings) -> str | None:
    return settings.public_url.rstrip("/") + "/auth/callback" if settings.public_url else None


def build_oauth(settings: Settings) -> OAuth | None:
    if not settings.entra_enabled:
        return None
    oauth = OAuth()
    oauth.register(
        name="entra",
        client_id=settings.azure_client_id,
        client_secret=settings.azure_client_secret,
        server_metadata_url=metadata_url(settings.azure_tenant_id),
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    return oauth


def verified_identity(claims, settings: Settings) -> EntraIdentity | None:
    """The identity if the (already signature-verified) claims are for this tenant and app.

    Who may sign in is decided by roles, not by an allow-list here.
    """
    if not isinstance(claims, dict):
        return None
    if claims.get("tid") != settings.azure_tenant_id:
        return None
    if claims.get("iss") != issuer(settings.azure_tenant_id):
        return None
    aud = claims.get("aud")
    if isinstance(aud, list):
        # multi-audience token: the authorised party must be this client too
        if settings.azure_client_id not in aud or claims.get("azp") != settings.azure_client_id:
            return None
    elif aud != settings.azure_client_id:
        return None

    def clean(value) -> str:
        return value.strip() if isinstance(value, str) else ""

    oid = clean(claims.get("oid"))
    # Only preferred_username may match or bind an existing account; the `email` claim
    # (possibly unverified, e.g. guests) is display-only.
    username = clean(claims.get("preferred_username"))
    upn = username or clean(claims.get("email"))
    if not oid or not upn:
        return None
    roles = claims.get("roles")
    groups = claims.get("groups")
    return EntraIdentity(
        tid=claims["tid"], oid=oid, upn=upn, name=clean(claims.get("name")) or upn,
        roles=[r for r in roles if isinstance(r, str)] if isinstance(roles, list) else [],
        groups=[g for g in groups if isinstance(g, str)] if isinstance(groups, list) else None,
        matchable=bool(username),
    )


def _bind(users: UserStore, rec: UserRecord, identity: EntraIdentity) -> UserRecord | None:
    """Bind the identity to a pre-created account; None if someone else got there first."""
    try:
        if users.bind_entra(rec.id, identity.tid, identity.oid):
            return users.get(rec.id)
    except DuplicateUserError:  # this identity is already bound to another row
        pass
    rec = users.by_entra(identity.tid, identity.oid)
    return rec


def _pre_created(users: UserStore, identity: EntraIdentity) -> UserRecord | None:
    """The unbound Entra account for this address, if the identity may claim it."""
    pre = users.by_email("entra", identity.upn)
    if pre is None:
        return None
    if pre.entra_oid is not None:  # same address, different person: never take over
        log.warning("Refused Microsoft sign-in for %s: the address belongs to a different "
                    "Microsoft account", identity.upn)
        return None
    if not identity.matchable:
        log.warning("Refused Microsoft sign-in for %s: the token has no preferred_username, "
                    "so it can't claim a pre-created account", identity.upn)
        return None
    return pre


def sign_in_entra(users: UserStore, identity: EntraIdentity, settings: Settings,
                  group_mappings: list[dict]) -> UserRecord | None:
    """Find or create the account for a verified identity and apply its mapped role.
    None means the person may not sign in (disabled, or the address belongs to someone else)."""
    audit = users._store.add_audit
    mapped = mapped_role({"roles": identity.roles, "groups": identity.groups}, group_mappings)
    default, default_pinned = default_role_for_new(settings.default_role)
    role, pinned = (mapped, False) if mapped is not None else (default, default_pinned)
    # Ruling 9: without a groups claim (overage) a group-derived role can't be recomputed.
    groups_unknown = identity.groups is None and bool(group_mappings)

    rec = users.by_entra(identity.tid, identity.oid)
    if rec is None:
        if users.by_email("entra", identity.upn) is not None:
            pre = _pre_created(users, identity)
            rec = _bind(users, pre, identity) if pre is not None else None
        else:
            try:
                rec = users.create("entra", identity.upn, identity.name, role.value,
                                   pinned=pinned, entra_tid=identity.tid,
                                   entra_oid=identity.oid)
            except DuplicateUserError:  # two first sign-ins at once: continue with the winner
                rec = users.by_entra(identity.tid, identity.oid)
                if rec is None:
                    pre = _pre_created(users, identity)
                    rec = _bind(users, pre, identity) if pre is not None else None
            else:
                audit(identity.upn, "users",
                      f"User created from Microsoft sign-in: {identity.upn} ({role.value})")
                return rec
    if rec is None or rec.disabled:
        return None

    changes: dict = {}
    if rec.name != identity.name:
        changes["name"] = identity.name
    if identity.matchable and rec.email != identity.upn:
        changes["email"] = identity.upn
    if not rec.role_pinned:
        if groups_unknown:
            log.warning("No groups in the token for %s; keeping their current role",
                        identity.upn)
        else:
            if role.value != rec.role:
                changes["role"] = role.value
            if pinned and not rec.role_pinned:
                changes["role_pinned"] = True
    return _apply(users, rec, changes, audit)


def _apply(users: UserStore, rec: UserRecord, changes: dict, audit) -> UserRecord:
    """Write the refreshed fields; a clash on the address or a would-be last-Admin demotion
    is skipped rather than failing the sign-in."""
    if not changes:
        return rec
    old_role = rec.role
    try:
        updated = users.update(rec.id, **changes)
    except LastAdminError:
        log.warning("Keeping the last active Admin %s as Admin despite their Microsoft role",
                    rec.email)
        return _apply(users, rec, {k: v for k, v in changes.items()
                                   if k not in ("role", "role_pinned")}, audit)
    except DuplicateUserError:
        log.warning("Couldn't update the address of %s to %s: another account uses it",
                    rec.email, changes.get("email"))
        return _apply(users, rec, {k: v for k, v in changes.items() if k != "email"}, audit)
    if "role" in changes:
        audit(updated.email, "users",
              f"Role changed by Entra mapping: {updated.email} {old_role} → {changes['role']}")
    return updated


def setup_entra(app: FastAPI) -> None:
    app.state.oauth = build_oauth(app.state.settings)

    def not_set_up(settings: Settings):
        if settings.azure_tenant_id and settings.azure_client_id and settings.azure_client_secret:
            return _page(NOT_SET_UP, "Set the dashboard address on the Settings page first.", 404)
        return _page(NOT_SET_UP, "Ask the administrator to set up Microsoft sign-in.", 404)

    @app.get("/auth/entra", include_in_schema=False)
    async def login(request: Request):
        oauth = app.state.oauth
        if oauth is None:
            return not_set_up(app.state.settings)
        try:
            return await oauth.entra.authorize_redirect(
                request, redirect_uri(app.state.settings))
        except (OAuthError, AuthlibBaseError, JoseError, httpx.HTTPError, ValueError) as exc:
            log.warning("Starting Microsoft sign-in failed (%s)", type(exc).__name__)
            return _page(
                "Sign-in failed",
                "Sign-in could not be completed. <a href=\"/auth/login\">Try again</a>.",
                502,
            )

    @app.get("/auth/callback", include_in_schema=False)
    async def callback(request: Request):
        oauth = app.state.oauth
        if oauth is None:
            return not_set_up(app.state.settings)
        setup_flow = bool(request.session.get("setup_entra"))
        try:
            token = await oauth.entra.authorize_access_token(request)
        except (OAuthError, AuthlibBaseError, JoseError, httpx.HTTPError) as exc:
            log.warning("Microsoft sign-in failed (%s)", type(exc).__name__)
            request.session.clear()
            return _page(
                "Sign-in failed",
                "Sign-in could not be completed. <a href=\"/auth/login\">Try again</a>.",
                400,
            )
        settings = app.state.settings
        identity = verified_identity(token.get("userinfo") or {}, settings)
        request.session.clear()  # always start fresh (session fixation)
        record = None
        if identity is not None:
            if setup_flow:
                # Ruling 3: first-start setup installs this hook; without it nobody signs in.
                hook = getattr(app.state, "entra_setup_hook", None)
                record = hook(identity) if hook is not None else None
            else:
                record = sign_in_entra(app.state.users, identity, settings,
                                       app.state.settings_service.group_mappings())
        if record is None and setup_flow:
            setup = getattr(app.state, "setup", None)
            if setup is not None and not setup.is_open():
                # Lost the first-start race (or it finished while signing in): not "no access".
                return _page("Setup has already been completed",
                             "Someone else finished setting up the dashboard. "
                             "<a href=\"/auth/login\">Sign in</a>.", 409)
        if record is None or record.disabled:
            return _page(
                "You don't have access to this dashboard",
                "Your account is not permitted to use this dashboard. "
                "Ask the administrator for access.",
                403,
            )
        return complete_sign_in(request, record, method="entra")


async def check_settings(http: httpx.AsyncClient, settings: Settings) -> bool:
    """Fetch the tenant's OIDC metadata and check it names the expected issuer."""
    if not settings.azure_tenant_id:
        return False
    try:
        resp = await http.get(metadata_url(settings.azure_tenant_id))
        resp.raise_for_status()
        return resp.json().get("issuer") == issuer(settings.azure_tenant_id)
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning("Microsoft sign-in settings test failed (%s)", type(exc).__name__)
        return False
