# Docker Manager – Settings, Accounts, Notifications and Checking (0.2–0.5)

Date: 2026-10-18
Status: Draft for approval
Builds on: [2026-10-17-docker-manager-design.md](2026-10-17-docker-manager-design.md)

## Intent

Docker Manager 0.1 works, but setting it up means an Entra app registration, a hand-written `.env` and a compose file full of variables, and changing anything means editing files on the server and restarting. Only Entra sign-in is supported, everyone who can sign in can do everything, and the only notification is a weekly ACS email.

This round makes the dashboard something the owner can install on a new server in minutes and then manage from the browser:

1. **Settings page** – most configuration moves out of `.env` into the dashboard, stored in its database.
2. **Accounts** – local accounts out of the box, Entra SSO as an option, and roles so that new people get read-only access until an admin says otherwise.
3. **Notifications** – email through Azure Communication Services, SMTP2GO or any SMTP server, for more events than the weekly digest; chat apps on the roadmap.
4. **Better checking** – ideas taken from [Diun](https://crazymax.dev/diun/) and [diun-dash](https://github.com/orkaa/diun-dash): per-container labels, skipping versions, release-notes links, filters, a heartbeat ping, and a **layout check** that everything a container uses lives under `/docker`.

The why is unchanged – **security and low effort**: servers stay current, and the dashboard itself must never become the weak point. It has the Docker socket, which is root on the server.

## What changes from the 0.1 design (owner's decisions)

The 0.1 design ruled several of these out. The owner has now asked for them, so these replace the matching 0.1 lines:

| 0.1 said | Now |
|---|---|
| Configuration via env vars only, no config UI | Settings page; env vars still work and win when set |
| Sign-in with Entra only; the dashboard has no user management | Local accounts **and** Entra SSO; a Users page; roles |
| No setup mode or default password | A one-time, token-protected setup step on first start (see "First start"). Still no default password |
| Weekly ACS email only | Notification channels: ACS, SMTP2GO, SMTP; more events |

Also decided by the owner on 2026-10-18: the first admin is created with a **setup token** (no default password); roles are **Viewer / Operator / Admin**; two-step sign-in is in this round and **required for local Admin accounts**.

Still non-goals in this round: managing several servers from one dashboard, unattended updates, rollbacks, start/stop/restart, creating stacks. Some of these are on the roadmap at the end.

## Goals

- A new install is safe before anyone has signed in: nothing but the setup page is reachable, and that needs a token only someone with server access can read.
- Every setting an admin is likely to change is editable in the browser, takes effect without a restart, and has a **Test** button where that makes sense.
- Existing 0.1 installs keep working after upgrading with **no changes** to their compose file or `.env`.
- Three roles – Viewer, Operator, Admin. New SSO users get a configurable default role (Viewer out of the box).
- Email notifications through ACS, SMTP2GO (HTTP API) or SMTP, for: weekly digest, updates found, update succeeded/failed, check failing, layout problems, user waiting for approval.
- Per-container control of what is checked, and a way to skip a version.
- A layout check that flags anything a container stores outside `COMPOSE_ROOT`.

## Anti-goals (failure even if it "works")

- Any page or API reachable without signing in, other than `/setup` (with the token), the sign-in pages and static files.
- A default or well-known password anywhere – in code, examples or docs.
- A secret (client secret, SMTP password, API key, connection string) ever shown again in the UI after saving, written to logs, or included in History.
- A Viewer being able to change anything, or to read job logs (update output can contain env values).
- Upgrading from 0.1 locking the owner out.
- Notification noise: the same "update available" sent every 6 hours.

## Constraints

- Same stack as 0.1: Python 3.12, FastAPI, Jinja2 server-rendered pages, a little vanilla JS, SQLite in `/data`, APScheduler. No Node build step.
- UK English in the UI, emails and docs.
- The app stays behind a reverse proxy with no `ports:` in the examples.
- Public repo: no secrets in code or examples.

---

## 1. Settings

### Where settings live

Two kinds:

**Bootstrap (env only)** – needed before the database opens, or tied to how the container is mounted:

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | `/data` | |
| `COMPOSE_ROOT` | `/docker` | Must match the volume mount |
| `SECRET_KEY_FILE` | `/data/secret.key` | Encryption + session key; see below |
| `PUBLIC_URL` | – | Optional for a token setup: if unset, taken from the setup request (which only the token holder can make) and saved as an editable setting. **Required** when `INITIAL_ADMIN_*` is used, because no setup request happens. Never taken from an ordinary request's headers |

**Runtime (Settings page, stored in SQLite)** – everything else from 0.1, plus the new settings:

- General: server name, timezone.
- Checking: check interval, random delay (minutes, default 0), check on startup (default on), watch stopped containers (default on – matches 0.1), health timeout, Mailcow folder.
- Registries: list of registry logins (host, username, password/token), replacing `REGISTRY_AUTH_FILE` (which still works).
- Layout check: on/off, named volumes allowed or flagged, extra allowed paths.
- Sign-in: local accounts on/off, Entra on/off with tenant/client/secret, default role, role mappings.
- Notifications: channels and events (section 3).
- Heartbeat: Healthchecks-style URL.

### Env still wins

Every runtime setting **that existed in 0.1** can still be set by its 0.1 env var name (`SERVER_NAME`, `CHECK_INTERVAL_HOURS`, `AZURE_TENANT_ID`, `ACS_CONNECTION_STRING`, `DIGEST_TO` …). When an env var is set:

- its value is used, whatever the database says;
- the field shows as read-only with "Set by the server's environment (`NAME`)".

This is how 0.1 installs keep working with no migration, and how people who prefer config-as-code keep it. Settings new in this round (SMTP, SMTP2GO, role and group mappings, layout allowlist, heartbeat …) are set in the UI only (Claude's call; env names can be added later if wanted).

### Secrets

- On first start, if `SECRET_KEY_FILE` doesn't exist, generate 32 random bytes and write it with mode `0600`. Session signing and secret encryption keys are derived from it (HKDF, separate labels).
- `SESSION_SECRET` from 0.1 still works if set, for the session key only.
- Secret fields are stored encrypted (Fernet / AES-GCM via `cryptography`). The UI never sends a saved secret back to the browser – it shows "Saved – enter a new value to replace it".
- README states plainly: this protects a copied database file on its own, not someone with all of `/data`. Putting the key file outside `/data` (via `SECRET_KEY_FILE` and a separate mount) is the stronger option.

### Behaviour

- Saving re-validates (e.g. a cron schedule, an email address, a timezone), applies at once (reschedules jobs, rebuilds the email sender and the Entra client) and records "Settings changed: <section> – <field names>" in History with who and when. Values are never recorded for secrets; for other fields old → new is fine.
- **Test** buttons: send a test email per channel, test Entra configuration (fetch the OIDC metadata and check the tenant), test a registry login, send a heartbeat.
- Only Admins see Settings.

## 2. Accounts and sign-in

### First start

When the database has **no users** and this is **not an upgrade from 0.1** (see below), whether or not Entra env vars are set:

1. Generate a one-time **setup token** (32 bytes, URL-safe). Write it to `/data/setup-token` (mode `0600`) and log it once: `Setup: open https://<host>/setup and enter the token from /data/setup-token (also: docker logs docker-manager)`.
2. Every request except `/setup`, `/static/*` and `/healthz` redirects to `/setup`.
3. `/setup` asks for the token, then for the first admin: either a local account (name, email, password, and two-step sign-in), or – if Entra is configured – "Sign in with Microsoft", which makes that Entra user the first Admin.
4. Creating the admin deletes the token file and closes `/setup` for good (it returns 404 afterwards).
5. Tokens don't expire on their own (the server could be left a while before someone visits), but are rate limited like passwords (section "Local accounts").

**Upgrading from 0.1:** an upgrade is recognised by an existing `dashboard.db` that has the 0.1 tables (`jobs`, `check_results`) and no schema-version table yet (Claude's call). That marks the database as an upgrade once, at migration. There is no setup step: Entra users sign in as now and the 0.1 allow rules are migrated (see "Migrating 0.1 allow rules"). The owner can add a local break-glass admin later. A **new** install with Entra env vars still goes through the setup token.

**Unattended installs:** `INITIAL_ADMIN_EMAIL` plus `INITIAL_ADMIN_PASSWORD_FILE` (a file, not a plain env value) create the first admin on first start instead of the token. Ignored once any user exists. The user is flagged "must change password" on first sign-in.

**Locked out:** `docker exec docker-manager docker-manager reset-admin <email>` – creates or resets a local admin and prints a one-time password that must be changed at sign-in.

### Local accounts

- Passwords: argon2id (`argon2-cffi`), minimum 12 characters, no composition rules, rejected if in a bundled list of common passwords (top 10k).
- Sign-in rate limit: 5 failures per account per 15 minutes → 15-minute lock; 20 failures per IP per 15 minutes → IP blocked for 15 minutes. Same response for "no such user" and "wrong password".
- **Two-step sign-in (TOTP)**: optional per user; an Admin setting can require it for Admins (default: required for Admins). Ten single-use recovery codes, stored hashed.
- Admins create local users with a one-time password the user must change, or a one-time invite link (valid 72 hours).
- Users can change their own password and TOTP from an Account page.

### Entra SSO

- Same OIDC code flow + PKCE and token checks as 0.1, now configured in Settings (or env).
- Settings page shows the exact redirect URI to paste into Azure.
- Users are matched by Entra object ID (`oid`) + tenant, not by email; their display name and UPN are refreshed at each sign-in.
- **Pre-created Entra users** (migrated from `ALLOWED_EMAILS`, or added by an Admin by UPN) are bound to their `oid` the first time someone signs in with that UPN **in the configured tenant**, and matched by `oid` from then on (Claude's call: this is exactly what 0.1 trusted, and only happens once per user).
- Optional: "Hide local sign-in for non-admins" – when Entra is on, the sign-in page shows "Sign in with Microsoft" and a small "Admin sign-in" link for local break-glass accounts.

### Roles

| Role | Can |
|---|---|
| **Viewer** | Dashboard, update status, History list (without job logs) |
| **Operator** | Viewer + Check now, update containers/projects/versions, skip/snooze versions, see job logs |
| **Admin** | Operator + Settings, Users, Mailcow update, dashboard self-update, send digest now |

- Every route declares the role it needs; the guard refuses (403) otherwise. The UI hides actions the user can't take.
- **Default role for new SSO users** (Settings): *Waiting for approval* (no access, listed for Admins to approve) / *Viewer* (default) / *Operator*. Never Admin – except the one-off legacy value described under "Migrating", which disappears from the choices once changed.
- **Role mappings for Entra**, checked at every sign-in for users whose role is **not pinned**. The user gets the **highest** role from all matching sources (Claude's call), falling back to the default role if nothing matches. So mappings can promote and demote:
  - Entra **app roles** (`roles` claim) named `DockerManager.Admin`, `DockerManager.Operator`, `DockerManager.Viewer`. README shows how to add them to the app registration. Preferred: no group-overage problem.
  - Entra **group object ID → role** list.
- **Pinning** (Claude's call): any role an Admin chooses by hand – approving a waiting user, setting a role on the Users page, and every role created by the 0.1 migration – is **pinned automatically**, and mappings leave it alone. The Users page shows "Pinned" or "From Entra", with "Let Entra decide" to unpin.
- Users can be disabled; disabling ends their sessions at once.
- There must always be at least one active Admin: the last Admin can't be demoted, disabled or deleted.

### Sessions

- Move to **server-side sessions** in SQLite: the cookie carries a random session ID only (still `HttpOnly`, `Secure`, `SameSite=Lax`). This makes disable, role change, password change and "sign out everywhere" take effect immediately.
- Lifetime: 8 hours idle, 7 days absolute. CSRF as in 0.1.
- Role is read from the user record on every request, not stored in the session.
- "Two-step sign-in required for Admins" applies to **local** accounts only. Entra sign-ins rely on Entra's own MFA / Conditional Access (Claude's call).

### Migrating 0.1 allow rules

- `ALLOWED_EMAILS` → each listed UPN is pre-created as an Entra user with role **Admin**, pinned (in 0.1 they could do everything, so this keeps their access).
- `ALLOWED_GROUP_ID` → a group mapping to **Admin**.
- With neither set (access controlled only by Azure assignment): the default role is set to a one-off value, **Admin (as in 0.1)**, so nobody loses access. A banner for Admins says "Everyone who can sign in is an Admin – review Users and the default role in Settings". Once an Admin changes the default role, this value is gone from the choices and the banner goes away. Users who signed in while it was set keep Admin, pinned.

### Audit

History gains sign-in events (success, failure, lockout), user changes, role changes and settings changes, alongside update jobs, with a filter by kind. **Only Admins see these audit entries**; Viewers and Operators see update jobs only (Viewers without logs) (Claude's call).

### Health endpoint (`/healthz`)

Added by the owner on 2026-10-18, for 0.3.

- `GET` / `HEAD /healthz` – **public** (no sign-in, no CSRF), so the reverse proxy, Uptime Kuma or Docker can probe it. It is reachable during first-start setup too.
- Returns `200` with `{"status": "ok"}` when the app is serving and the database answers a trivial query; `503` with `{"status": "unavailable"}` otherwise. `Cache-Control: no-store`.
- Reveals nothing else: no version, server name, container counts, check results or error text (Claude's call – it is public, and the Docker socket makes this a sensitive host).
- Docker or registry problems do **not** make it fail: those show on the dashboard and through the "Check failing" notification; `/healthz` answers "is the dashboard itself up" (Claude's call).
- Not logged per request at INFO, so frequent probes don't fill the logs.
- The image gets a Docker `HEALTHCHECK` that calls it (with Python's standard library – no `curl` in the image), every 30 s. The self-update helper container (same image, different entrypoint) disables that health check.
- README documents it for Caddy/Traefik/Uptime Kuma monitoring.

## 3. Notifications

### Channels and events

A **channel** = a provider + its settings + recipients + which events it gets. Several channels are allowed (e.g. digest to one address, failures to another).

Events:

| Event | When | Default |
|---|---|---|
| Weekly digest | `DIGEST_SCHEDULE` (now a setting) | on |
| Updates found | A check finds updates **that weren't in the previous notification** | off |
| Update finished | A job succeeds / fails / rolls back | failures only |
| Check failing | The scheduled check has failed 2 times in a row; and when it recovers | on |

A **failed check** means the check as a whole didn't work: it raised, Docker couldn't be reached, or **every** registry lookup failed. One image's registry error (a rate limit, one private registry down) is not a failed check – it shows on that row and in the digest's "Couldn't check" section as today (Claude's call). The heartbeat uses the same rule.
| Layout problems | New problems found by the layout check | on |
| User waiting for approval | A new SSO user lands in "waiting for approval" | on |

"Updates found" remembers what it last reported per container (image + digest/tag) so the same update isn't sent again every check.

### Email providers (this round)

- **Azure Communication Services** – as 0.1 (connection string, sender address).
- **SMTP2GO** – HTTP API (`https://api.smtp2go.com/v3/email/send`, API key, sender address). Clearer errors than SMTP.
- **SMTP** – host, port, security (STARTTLS / TLS / none – "none" only allowed to private addresses), username, password, sender. Presets fill in host/port for SMTP2GO (`mail.smtp2go.com`, 2525/587) and Microsoft 365 (`smtp.office365.com`, 587).

All emails use the existing digest look; new short templates for the other events, each with a link to the dashboard.

**0.1 email env vars:** if `ACS_CONNECTION_STRING`, `DIGEST_FROM` and `DIGEST_TO` are set, they appear as a read-only channel "Email (from environment)" that gets the **weekly digest only**, exactly as in 0.1, so upgraded installs get no new emails they didn't ask for (Claude's call). Admins can add other channels for other events.

### Roadmap channels (not this round)

Design the provider interface so these drop in without touching the event code:

- Microsoft Teams via a **Workflows** webhook (the old Office 365 connector webhooks are retired).
- Slack incoming webhook, Discord webhook.
- ntfy, Gotify, Pushover, Telegram.
- Generic webhook with a JSON body.
- Apprise URL (covers ~100 services through one setting).

## 4. Checking improvements

### Per-container labels

Read from container labels (set in compose files). The same can be set per container in the UI (stored in the database); a label wins over the UI.

| Label | Meaning |
|---|---|
| `docker-manager.enable=false` | Don't check this container (shown greyed as "Not watched") |
| `docker-manager.include_tags=<regex>[;<regex>]` | Only these tags count as newer versions |
| `docker-manager.exclude_tags=<regex>[;<regex>]` | Never offer these tags |
| `docker-manager.notify=false` | Check, but leave out of notifications |
| `docker-manager.release_notes=<url>` | Link shown next to the update |
| `docker-manager.layout.ignore=true` | Skip the layout check for this container |

Regexes are matched against the whole tag, with a time limit so a bad pattern can't hang the check.

### Skip and snooze

On any update an Operator can choose **Skip this version** (hidden until a newer one appears) or **Snooze** (1 week / 1 month). Shown under a "Skipped" filter, reversible, and recorded in History with who and an optional reason.

### Release notes links

Shown next to an update: the `release_notes` label if set; else the image's `org.opencontainers.image.source` label if it points at GitHub (link to `/releases`); else the Docker Hub page for Hub images.

### Dashboard filters and search

Search by container/image/project name; filters: Needs update, Major only, Stopped, Not checkable, Skipped, Layout problems. Saved per browser.

### Scheduling

- **Random delay**: each scheduled check starts up to N minutes late (setting, default 0) so many servers don't hit Docker Hub at once.
- **Check on startup**: as 0.1 (setting, default on).

### Heartbeat

Optional URL pinged after every scheduled check: `<url>` on success, `<url>/fail` on failure (Healthchecks.io convention; works with Uptime Kuma push URLs too). Lets the owner find out if the checker itself stops.

### Layout check

Runs with every check. For each compose-managed container, inspect `Mounts` and the compose labels, and flag:

| Problem | Example | Severity |
|---|---|---|
| Bind mount outside `COMPOSE_ROOT` | `/srv/app/data:/data` | Problem |
| Named volume | `pgdata:/var/lib/postgresql/data` (data lives in `/var/lib/docker/volumes`) | Problem, or allowed – setting (default: Problem) |
| Compose file outside `COMPOSE_ROOT` | project started from `/root/app` | Problem |
| Env file outside `COMPOSE_ROOT` | `--env-file /etc/app.env` | Problem |
| Not started by compose | `docker run …` container | Notice |

Allowed without a flag: `/var/run/docker.sock`, `/run/docker.sock`, `/etc/localtime`, `/etc/timezone`, `/dev/*`, `/proc`, `/sys` (read-only), anonymous volumes, tmpfs, the Mailcow folder and Mailcow's own volumes, the dashboard's own `/data` mount. Plus an editable allowlist in Settings and the `layout.ignore` label.

Shown as a badge on each project row ("2 mounts outside /docker"), a **Layout** page listing every problem with a plain fix hint (e.g. "Move the data to `/docker/<project>/data` and change `pgdata:/var/lib/postgresql/data` to `./data:/var/lib/postgresql/data`"), a section in the digest, and the "Layout problems" event. It never changes anything.

## Release order

Each is a releasable version with its own CHANGELOG entry:

1. **0.2 – Foundations:** settings store with env override, secret key and encryption, server-side sessions, Settings page (General, Checking, Registries, Email as today), History for settings changes. No behaviour change for existing installs.
2. **0.3 – Accounts:** first-start setup, local accounts, TOTP, Entra in Settings, roles, Users page, migration of 0.1 allow rules, `reset-admin`, `/healthz`.
3. **0.4 – Notifications:** channels, ACS/SMTP2GO/SMTP, events, test buttons.
4. **0.5 – Checking:** labels, skip/snooze, release-notes links, filters/search, random delay, heartbeat, layout check.

## Roadmap (after 0.5, not designed here)

- **Create and edit stacks** (Admin only): new `/docker/<name>/compose.yml` + `.env` edited in the browser, validated with `docker compose config`, previewed (images, ports, mounts), refused if it fails the layout check unless an Admin overrides (recorded), then deployed with live output. Edit existing stacks with a diff preview and backups.
- Restore the last compose-file backup ("roll back") from History.
- Restart / stop / start a container; read-only recent logs.
- Image prune and disk usage.
- Optional auto-update per container in a maintenance window.
- Generic OIDC sign-in (Authentik, Keycloak, Authelia, Google, Okta), then trusted-header auth behind a proxy.
- Chat notification channels (section 3).
- Hub view across servers using per-server API tokens; Prometheus `/metrics`.

## Left to the builder

- Database schema and migrations (a simple numbered-migration table is fine).
- Exact page layouts, as long as they match the 0.1 look and work on a phone.
- Library choices within the stack (`cryptography`, `argon2-cffi`, `pyotp`, `aiosmtplib` or `smtplib` in a thread).
- Which provider-specific error messages to surface.
