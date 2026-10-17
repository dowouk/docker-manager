# Docker Manager – Design

Date: 2026-10-17
Status: Draft for approval

## Intent

The owner runs several Hostinger VPS servers, each with Docker containers started by `docker compose`. Today there is no way to see whether a container is running the newest image, so servers drift onto old, potentially vulnerable versions.

This project is a small web dashboard, packaged as a single Docker container, that runs **on each server separately** and:

1. shows every container on that server, the image version it is running and the newest available version;
2. lets a signed-in user update a container with one click;
3. handles the Mailcow mail stack through Mailcow's own `update.sh` rather than `docker pull`;
4. emails a weekly digest of available updates.

The why: **security**. Success is every server running current images with minimal effort, and the owner being told when that is not the case.

## Goals

- One dashboard per server, seeing only that server's containers. No central/multi-server view.
- Detect two kinds of update for every container:
  - **Newer build** – the tag in use (e.g. `nginx:latest`, `postgres:16`) has a newer image in the registry than the one the container is running (digest differs).
  - **Newer version** – the tag is a pinned version (e.g. `postgres:16.2`) and newer version tags exist in the registry (e.g. `16.4`, `17.0`).
- One-click updates:
  - Newer build: `docker compose pull <service>` then `docker compose up -d <service>` in that container's compose project.
  - Newer version, **minor or patch only** (e.g. `16.2` → `16.4`): back up the compose file, rewrite the image tag for that service, then pull and `up -d`.
  - Newer version, **major** (e.g. `16` → `17`): shown with a warning that it may need a data migration. No button – done by hand.
  - Mailcow: run Mailcow's `update.sh` non-interactively.
- Live output of an update streamed to the page while it runs, with success/failure shown at the end.
- Checks run automatically on a schedule (default every 6 hours) plus a "Check now" button.
- Weekly email digest via **Azure Communication Services (ACS) Email**, listing available updates on that server.
- Sign-in with **Microsoft Entra ID (Azure AD)**. Who may sign in is controlled in Azure by assigning users to the Entra app ("assignment required"); the dashboard itself has no user management.

## Non-goals

- Managing several servers from one dashboard.
- Automatic, unattended updating. Every update is a human click.
- Major-version upgrades, database migrations or rollbacks.
- Managing containers not started by compose beyond displaying them (they are shown, marked "not managed by compose", with no update button).
- Starting/stopping/restarting containers or general Docker management (this is not Portainer).
- Vulnerability scanning (CVE lists). Possibly later; out of scope now.

## Anti-goals (would count as failure even if it "works")

- Anything reachable without an Entra sign-in. The container has Docker socket access, which is root-equivalent on the host. Every page and API route, including the live-output stream, must require an authenticated session; there is no unauthenticated fallback, "setup mode" or default password.
- Getting the owner rate-limited by Docker Hub, which would break their own pulls.
- Silently editing compose files (always back up, always show what changed).
- Breaking Mailcow by running its update in a way that differs from running it by hand.
- A noisy digest that cries wolf (e.g. listing `-rc`, `-beta`, or differently-flavoured tags as updates).

## Constraints and environment

- Host: Hostinger VPS, Linux, Docker Engine with the Compose v2 plugin.
- Reverse proxy: **Caddy on some servers, Traefik on others.** The dashboard must work behind either: plain HTTP inside, trust `X-Forwarded-Proto`/`X-Forwarded-Host` for building the OAuth redirect URL. Provide example config for both (Caddyfile snippet, Traefik labels).
- **All compose projects live under `/docker`** on every server (one folder per project, e.g. `/docker/<project>/docker-compose.yml`). They are deployed with `docker compose` and also appear in **Hostinger's hPanel Docker Manager**, which uses the same `/docker` folders (owner's answer). The dashboard mounts `/docker:/docker` (same path). Assumption for the builder to verify on a real server: hPanel reads the compose file from `/docker/<project>` and won't overwrite an edited tag; if hPanel turns out to keep its own copy, minor/patch tag edits must be disabled for those projects and shown as "update in hPanel".
- Projects outside `/docker` (e.g. Mailcow) are discovered via their compose labels; if their folder isn't mounted, they are shown with "project files not mounted" and no update button.
- Mailcow lives on at least one server, normally at `/opt/mailcow-dockerized` (configurable). The owner updates it by hand today with plain `./update.sh` as root, on the stable branch – the dashboard must do the same, with only `--force` added to skip the prompts.
- Configuration via environment variables only (no config UI), suitable for a compose file.
- **Public** GitHub repo; image published as a public package to GitHub Container Registry (`ghcr.io/<owner>/docker-manager`), multi-arch (amd64, arm64), built by GitHub Actions using the built-in `GITHUB_TOKEN` (owner's decision). No secrets or server-specific settings in the repo – everything is configured by env vars in each server's compose file.
- UK English in the UI and emails.

## Decisions made (by the owner)

- Per-server dashboards, no central view.
- Containers are run with `docker compose`.
- Public URL behind Caddy or Traefik, with Entra ID sign-in; access controlled via Azure app assignment.
- Detect both newer builds and newer pinned versions.
- Minor/patch version bumps get a button that edits the compose file (with backup); major bumps are shown only, with a warning.
- Mailcow updated via `update.sh`, not `docker pull`.
- Weekly digest by email via Azure Communication Services.

## Decisions made by Claude (owner can override)

- **Stack:** Python 3.12, FastAPI, server-rendered HTML (Jinja2) with a little vanilla JS / htmx for live output. No Node build step. SQLite for state (check results, update history, last digest sent), stored in a mounted volume.
- **Docker access:** Docker SDK for Python over the mounted `/var/run/docker.sock`; the `docker` CLI with the compose plugin is installed in the image for running compose commands.
- **Paths:** compose project folders are mounted into the container **at the same path as on the host** (e.g. `/opt/stacks:/opt/stacks`), because compose labels record host paths and relative bind mounts must resolve identically.
- **Schedule:** checks every 6 hours (configurable); digest Monday 08:00 Europe/London (configurable day/time/timezone). If there are no updates, the digest is still sent, saying "all up to date", so silence never means "broken". (Configurable to skip.)
- **Sessions:** signed, HTTP-only, `Secure`, `SameSite=Lax` cookie; 8-hour lifetime. State-changing actions are POSTs with CSRF protection.
- **Self-update:** the dashboard detects its own container and shows its update status, but updating itself is done via a detached one-shot helper container (see below) so the request is not killed mid-way.

## How it works

### Discovering containers

List all containers (running and stopped) through the Docker API. For each, read:

- image reference as written (`Config.Image`, e.g. `postgres:16.2-alpine`);
- image ID and the image's `RepoDigests`;
- compose labels: `com.docker.compose.project`, `com.docker.compose.project.working_dir`, `com.docker.compose.project.config_files`, `com.docker.compose.service`.

Group the UI by compose project. Containers without compose labels go in an "Other" group with no update actions.

Containers belonging to the Mailcow project (project name `mailcowdockerized`, or working dir equal to `MAILCOW_DIR`) are collapsed into a single **Mailcow** row; their individual images are not checked against registries.

### Checking for a newer build (digest comparison)

For each distinct image reference:

1. Resolve registry, repository and tag (Docker Hub default `docker.io/library/` normalisation; support `ghcr.io`, `lscr.io`, `quay.io`, `mcr.microsoft.com`, `gcr.io` and any other OCI v2 registry).
2. `HEAD /v2/<repo>/manifests/<tag>` with `Accept` headers for OCI index, Docker manifest list, and single manifests, to get `Docker-Content-Digest`. Use HEAD because Docker Hub does not count HEAD requests towards the pull rate limit.
3. Compare with the `RepoDigests` of the image the container is **actually running** (`Container.Image` ID), not whatever the tag currently points to locally. Otherwise a container whose image was pulled but never recreated would wrongly show as up to date. If the tag locally points to a newer image than the container runs, show "Pulled but not recreated" (actionable: `up -d`).
4. Handle registry token auth (the `WWW-Authenticate: Bearer` flow). Anonymous by default; optionally read credentials from a mounted Docker `config.json` for private registries.

Rate limits: manifest HEADs do not count against Docker Hub's pull limit, but the Hub tags API is separately rate-limited, and large library repos (`node`, `python`, `postgres`, `nginx`) have thousands of tags. Cache tag lists (at least one check interval, ideally 24 h), cap pages per repo (Hub: 10 pages of 100, newest first – anonymous requests get HTTP 403 beyond page 10, treated as the end of the list), share one fetch between concurrent checks of the same repo, cache failures for 15 min, and prefer the registry's `/v2/.../tags/list` where cheaper. Updates performed by the dashboard are real pulls and **do** count against Docker Hub's anonymous per-IP limit. The owner has decided **not** to use a Docker Hub account, so everything must work anonymously: keep registry traffic minimal, and if a pull hits the rate limit, report it clearly ("Docker Hub rate limit reached – try again later") rather than as a generic failure. Do not recommend Docker Hub credentials in the README.

Edge cases: images built locally (no `RepoDigests`) or referenced by digest (`image@sha256:…`) are shown as "Not checkable" with the reason.

### Checking for a newer version (tag comparison)

Only when the tag looks like a version (e.g. `16`, `16.2`, `v2.11.3`, `1.25.4-alpine`, `8.0-bookworm`):

1. List tags (`GET /v2/<repo>/tags/list`, following `Link` pagination; for Docker Hub use the Hub tags API which is faster and gives last-updated dates). Cache tag lists for the check interval.
2. Parse the current tag into: optional `v` prefix, numeric parts, and suffix (`-alpine`, `-bookworm`, etc.).
3. Candidate tags must: have the **same prefix style, same suffix, and same number of numeric parts**, and contain no pre-release markers (`rc`, `beta`, `alpha`, `dev`, `nightly`, `preview`, `snapshot`, case-insensitive).
4. Highest candidate greater than current → newer version. Classify:
   - same major, higher minor/patch → **minor/patch** (actionable);
   - higher major → **major** (warning only). If both exist, show both: the newest within the current major as actionable, and the newest overall as information.
5. Tags that are not version-like (`latest`, `stable`, `lts`, `mainline`) skip this step; only the digest check applies.

An image can be both: e.g. `postgres:16` can have a newer build of `16` *and* a newer major `17`.

### Mailcow

- Configured by `MAILCOW_DIR` (default `/opt/mailcow-dockerized`). If the folder isn't present/mounted, Mailcow handling is off.
- **Check:** run `./update.sh --check` in `MAILCOW_DIR`, **on the host** (same mechanism as the update below – it runs `git fetch`, requires root and checks for host tools). Exit code **0 = update available, 3 = no update, anything else = error**. Also show the current Mailcow version/tag (from the git checkout) and the latest release from the GitHub API (`mailcow/mailcow-dockerized` releases) for display.
- **Update:** run `./update.sh --force` (non-interactive), with no TTY and stdin closed so any stray prompt fails rather than hangs (with a timeout). `update.sh` can update itself and exit with **code 2** asking to be re-run; the runner re-runs it **once** on exit 2 (message text as fallback). Stream all output.
- If `update.sh` stops because of local changes or merge conflicts in the Mailcow git tree, show the failure clearly with the log; never try to resolve it automatically.
- **Where it runs:** to behave exactly like running it by hand, run it **on the host**, not inside the dashboard's container. Recommended mechanism: the dashboard container is started with `pid: host` and `privileged: true` (only on the Mailcow server, via a compose override/profile) and runs it in the host's namespaces with a clean environment and a login shell, so nothing leaks in from the container, e.g. `nsenter -t 1 -m -u -i -n -C -- env -i HOME=/root USER=root LOGNAME=root bash -lc 'cd /opt/mailcow-dockerized && ./update.sh --force'` (HOME/USER/LOGNAME set as for a root login) (directory passed explicitly, not expanded from container env). The builder may choose a different mechanism if it gives the same "exactly as by hand" result; document the trade-off.
- README wording: Docker socket access is **already** root-equivalent on every server; `pid: host`/`privileged` on the Mailcow server removes friction rather than adding a new class of risk. Don't imply non-Mailcow servers are meaningfully safer – the protection is the Entra sign-in.

### Updating a container

All update jobs:

- run one at a time per server (a simple job queue/lock; a second click while busy is queued or refused with a clear message);
- stream stdout/stderr to the browser (Server-Sent Events or WebSocket), and keep the full log in SQLite for the history view;
- re-check the affected images afterwards and refresh the row.

**Newer build:** in the project's working dir (with `PWD` set to it, so `${PWD}` in compose files resolves as by hand), with the project's config files, `-p <project>` and an `--env-file` for each file in the project's `com.docker.compose.project.environment_file` label (if one isn't readable, refuse: "Project uses an env file that isn't mounted – update by hand"):
`docker compose pull <service>` then `docker compose up -d --no-deps <service>`.
If several services in the project share the image, offer "update project" (pull + `up -d` for the whole project) as well as per-service – but not for a project that contains the dashboard itself.

**Minor/patch version bump:**
1. Find the compose file that defines the service's `image:` (from `config_files`; handle multiple files/overrides). If the `image:` line can't be found there (e.g. it comes from an `include:` or `extends: file:` file, which that label doesn't list), refuse the edit with a clear "update by hand" message.
2. Back it up to the dashboard's data volume, `/data/backups/<project>/<file>.YYYYMMDD-HHMMSS` (Claude's call: keeps `/docker` and the hPanel view free of clutter). Show the backup path in the job log.
3. Replace the tag on that service's `image:` line only, preserving the rest of the file byte-for-byte (comments, ordering, formatting). Do a targeted text edit, not a YAML load/dump round-trip. If the image is set via a variable (e.g. `image: postgres:${PG_VERSION}`) or from an `.env` file, do not edit – show "Version set by variable; update by hand".
4. Show the diff in a confirmation dialog **before** step 2; only proceed when confirmed.
5. Pull and `up -d` as above. If the pull fails, restore the backup. If `up -d` fails or the container doesn't reach running (or healthy, where a healthcheck exists) and stay there without restarting for 20 s, within `HEALTH_TIMEOUT_SECONDS` (default 300 s), restore the backup and run `up -d` again on the old tag, then report the failure with the log (Claude's call: leaving a broken service up is worse for a mail/web server than staying one version behind).

**Self-update:** the dashboard updates itself by starting a short-lived helper container (same image, socket mounted) that runs the pull/`up -d` for the dashboard's own service and exits. The helper is not started with `--rm`: the self-update job streams its output into the job log and reads its exit code – non-zero fails the job; zero with the dashboard still running 30 s later means "Already up to date – nothing to restart". The helper is removed afterwards (and any left over from an earlier self-update before the next one starts). When the dashboard is stopped mid-job, the job is left running and classified on the next start.

### Sign-in (Entra ID)

- OpenID Connect authorisation code flow with PKCE, using `msal` or `authlib`.
- Single-tenant: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`. Redirect URI `https://<host>/auth/callback`.
- Validate the ID token (issuer = tenant, audience = client ID, signature, expiry). Access control relies on "Assignment required" being enabled on the Enterprise Application; the README must say to turn this on and explain that without it any user in the tenant could sign in.
- Optional `ALLOWED_GROUP_ID` / `ALLOWED_EMAILS` env vars as a second safety net.
- Show the signed-in user's name and a sign-out link. Log who ran each update in the history.

### Weekly digest (Azure Communication Services Email)

- Env: `ACS_CONNECTION_STRING`, `DIGEST_FROM` (a verified ACS sender address), `DIGEST_TO` (comma-separated), `DIGEST_SCHEDULE` (cron-style, default Monday 08:00), `TZ`.
- Use the `azure-communication-email` Python SDK.
- Content: server name (`SERVER_NAME`), time of last check, a table of containers with updates grouped as Newer build / Minor-patch / Major / Mailcow, and a link to the dashboard. Also list anything "Not checkable" or any check errors, so failures are visible.
- A "Send digest now" button for testing.

### UI

- Single page: header with server name, last check time, "Check now", signed-in user.
- Summary counts (up to date / updates available / major available / errors).
- Table grouped by compose project: container, service, image, current tag, current digest (short), status badge, newest version, action button(s).
- Filters: "Show only updates".
- Update dialog with live log; history page listing past updates (when, who, what, result, log).
- Works on a phone.

## Configuration (env vars)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SERVER_NAME` | yes | – | Shown in UI and digest |
| `PUBLIC_URL` | yes | – | e.g. `https://updates.server1.example.com` |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | yes | – | Entra app |
| `SESSION_SECRET` | yes | – | Random string for cookie signing |
| `ALLOWED_GROUP_ID`, `ALLOWED_EMAILS` | no | – | Extra restriction |
| `CHECK_INTERVAL_HOURS` | no | `6` | |
| `COMPOSE_ROOT` | no | `/docker` | Must be mounted at the same path |
| `MAILCOW_DIR` | no | `/opt/mailcow-dockerized` | Mailcow off if no Mailcow containers; if they exist but the folder isn't mounted, the Mailcow row shows "Mailcow folder not mounted" |
| `ACS_CONNECTION_STRING`, `DIGEST_FROM`, `DIGEST_TO` | no | – | Digest off if absent |
| `DIGEST_SCHEDULE` | no | `0 8 * * MON` | Standard cron; weekday numbers mean what they do in cron (0/7 = Sun, 1 = Mon), names (`MON-FRI`) recommended |
| `DIGEST_WHEN_NONE` | no | `true` | Send "all up to date" digests (a digest listing "Couldn't check" items is always sent) |
| `TZ` | no | `Europe/London` | |
| `HEALTH_TIMEOUT_SECONDS` | no | `300` | Version-update health wait before rollback; healthy = running (and healthy if it has a healthcheck) with an unchanged restart count for 20 s |
| `REGISTRY_AUTH_FILE` | no | – | Path to mounted Docker-style `config.json` for private registries (not `DOCKER_CONFIG`, which the docker CLI reads as a directory) |

## Deliverables

- Source repo with Dockerfile, tests, GitHub Actions workflow to build and push a multi-arch image to GHCR.
- `README.md`: Azure app registration steps (redirect URI, client secret, assignment required), ACS email setup, and example compose files:
  - standard server behind **Caddy** (Caddyfile snippet);
  - standard server behind **Traefik** (labels);
  - Mailcow server variant (with `pid: host`, `privileged`, Mailcow dir mount) – and a clear warning about what those options mean.
- Tests: tag parsing/comparison (lots of real-world tag cases), compose-file tag editing (comments and formatting preserved, variable tags refused), registry digest/tag logic against mocked HTTP, auth required on every route.

## Left to the builder

- Exact library choices within the stack (htmx vs plain JS, `authlib` vs `msal`, scheduler library).
- Visual design, as long as it is clear and works on a phone.
- The mechanism for running `update.sh` on the host if a better one than `nsenter` exists, subject to the "exactly as by hand" requirement.
- Registry-specific shortcuts (e.g. Docker Hub tag API) and caching details, subject to staying well under rate limits.
