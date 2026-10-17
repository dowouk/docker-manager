# Docker Manager

A small web page, running on each of your servers, that shows whether your Docker containers are on the newest images – and lets you update them with one click.

- [What it does](#what-it-does)
- [How it decides there's an update](#how-it-decides-theres-an-update)
- [Security](#security)
- [Set up the Azure app registration](#set-up-the-azure-app-registration)
- [Set up Azure Communication Services email](#set-up-azure-communication-services-email)
- [Install on a server](#install-on-a-server)
- [Configuration reference](#configuration-reference)
- [Updating the dashboard itself](#updating-the-dashboard-itself)
- [Docker Hub limits](#docker-hub-limits)
- [First run checklist](#first-run-checklist)
- [Troubleshooting](#troubleshooting)

## What it does

You install one copy of the dashboard on **each** server. It only sees the containers on that server.

- Lists every container, grouped by compose project, with the image it runs and the newest one available.
- Checks automatically every 6 hours, and whenever you press **Check now**.
- Updates a container with one click and shows the live output while it runs. Every update is kept in **History** (who, when, what, result and the full log).
- Updates Mailcow using Mailcow's own `update.sh`, exactly as you would by hand.
- Sends a weekly email (Monday 08:00 by default) listing what needs updating. If everything is up to date it still emails, so silence never means "broken".
- Sign-in is with your Microsoft 365 / Entra ID account. There are no passwords to manage in the dashboard itself.

It never updates anything on its own – every update is a click by a signed-in person.

## How it decides there's an update

For each container it checks two things:

**Newer build** – the tag you use (for example `nginx:latest` or `postgres:16`) now points to a newer image in the registry than the one your container is running. The button runs `docker compose pull` and `docker compose up -d --no-deps` for that service only (the services it depends on are left alone). If several services in the project use the same image, there's also **Update whole project** (not offered for the dashboard's own project). A stopped container shows **Stopped – updating will start it**.

If the newer image has already been pulled but the container was never recreated, it shows **Pulled – not recreated**; the same button recreates it on the newer image.

**Newer version** – the tag is a version number (for example `postgres:16.2` or `1.25.4-alpine`) and a higher version exists. Only tags that look the same are compared – same `v` prefix or not, same ending (`-alpine`, `-bookworm` and so on) and the same number of parts – and test releases (`rc`, `beta`, `alpha`, `dev`, `nightly`, `preview`, `snapshot`) are ignored.

- **Minor or patch** (for example `16.2` → `16.4`): you get a button. You are shown exactly what will change in the compose file first. The file is backed up to the dashboard's data volume (`/data/backups/<project>/<file>.<date-time>`), the tag is changed on that one line only, and the service is pulled and restarted. It then has to stay running (and healthy, if it has a healthcheck) without restarting for 20 seconds, within `HEALTH_TIMEOUT_SECONDS`. If it doesn't, the backup is put back and the old version restarted.
- **Major** (for example `16` → `17`): shown with a warning that it may need a data migration. There's no button – do these by hand.

If the version comes from a variable (for example `image: postgres:${PG_VERSION}`), the dashboard won't edit it and asks you to update by hand.

Updates run `docker compose` the way you would by hand in the project's folder: `${PWD}` in a compose file is the project folder, and if you started the project with `--env-file`, the same files are passed again (they must be visible to the dashboard at the same path – anything under `/docker` is).

**Mailcow** – all Mailcow containers are shown as one **Mailcow** row. The dashboard runs Mailcow's own `./update.sh --check` on the server to see whether Mailcow has an update. **Update Mailcow** runs `./update.sh --force` – the same as running `./update.sh` by hand, without the questions.

Some containers show **Not checkable** – for example images you built yourself, or images pinned by digest (`image@sha256:…`). Containers not started by `docker compose` are shown but can't be updated here.

## Security

Please read this before installing.

- **The dashboard can control Docker on the server.** It needs the Docker socket (`/var/run/docker.sock`) to see and update containers, and access to that socket is the same as root access to the server. This is true on every server, not just the ones that run Mailcow.
- **The Entra sign-in is the protection.** Every page needs a signed-in account – there is no default password, setup mode or way in without signing in. That is why the [Assignment required](#5-only-let-assigned-people-sign-in) step below matters so much.
- **On a server that runs Mailcow** the dashboard also runs with `pid: host` and `privileged: true`, so it can run Mailcow's `update.sh` directly on the server, exactly as you would by hand as root. Because the Docker socket already gives root-level control, these options remove friction rather than adding a new kind of risk. Don't think of the other servers as much safer – on all of them, the sign-in is what protects you.
- The dashboard is only reachable through your reverse proxy (Caddy or Traefik). The examples have no `ports:` section on purpose – please keep it that way.
- Keep your `.env` file (secrets) private.

## Set up the Azure app registration

You need one app registration. You can use the same one for all your servers – just add a redirect URI for each.

### 1. Register the app

1. Sign in to the [Microsoft Entra admin centre](https://entra.microsoft.com).
2. Go to **Identity → Applications → App registrations → New registration**.
3. **Name:** `Docker Manager` (anything you like).
4. **Supported account types:** **Accounts in this organisational directory only (Single tenant)**.
5. **Redirect URI:** choose **Web**, and enter `https://<your-host>/auth/callback` – for example `https://updates.server1.example.com/auth/callback`.
6. Click **Register**.

### 2. Copy the IDs

On the app's **Overview** page, copy:

- **Application (client) ID** → `AZURE_CLIENT_ID`
- **Directory (tenant) ID** → `AZURE_TENANT_ID` (the long code like `1a2b3c4d-…` – not your domain name)

### 3. Add each server's address

For each extra server, go to **Authentication → Web → Redirect URIs → Add URI** and add `https://<that server's host>/auth/callback`. Click **Save**.

### 4. Create a client secret

1. Go to **Certificates & secrets → Client secrets → New client secret**.
2. Give it a description and choose how long it lasts (put a reminder in your calendar to renew it before it expires).
3. Click **Add**, then copy the **Value** straight away (it's only shown once) → `AZURE_CLIENT_SECRET`. Don't copy the "Secret ID" – that's a different thing.

### 5. Only let assigned people sign in

> **Important:** without this step, **anyone in your Microsoft 365 organisation could sign in** and control Docker on your servers.

1. Go to **Identity → Applications → Enterprise applications** and open the app with the same name.
2. Go to **Properties**, set **Assignment required?** to **Yes**, and click **Save**.
3. Go to **Users and groups → Add user/group**, choose yourself (and anyone else who should have access), and click **Assign**.

### 6. Optional: extra restrictions

These are a second safety net on top of assignment. You don't need them.

- **`ALLOWED_EMAILS`** – a comma-separated list of people allowed in. It is matched against their **sign-in name** (the user principal name, shown as "User principal name" in Entra), **not** their email address. Usually these are the same, but if someone signs in as `jo@company.onmicrosoft.com` and has the mail address `jo@company.co.uk`, you must list `jo@company.onmicrosoft.com`.
- **`ALLOWED_GROUP_ID`** – the **Object ID** of an Entra group whose members are allowed in. This needs the app to send group membership in the sign-in token: in the app registration, go to **Token configuration → Add groups claim**, tick **Security groups** (or **Groups assigned to the application**), and **Save**. Without a groups claim nobody can sign in when this is set.

Sign-in fails closed: if a restriction is set and the person doesn't meet it, they are refused.

## Set up Azure Communication Services email

This is only needed for the weekly email. Skip it if you don't want emails.

1. In the [Azure portal](https://portal.azure.com), create an **Email Communication Services** resource (search for "Email Communication Services" → **Create**). Choose a resource group and a data location (for example **UK** or **Europe**).
2. Open it and go to **Provision domains**. Either:
   - **Add an Azure managed domain** – quickest; it gives you an address like `DoNotReply@xxxx.azurecomm.net`, or
   - **Add a custom domain** (for example `example.com`), then add the TXT, SPF and DKIM records it shows you at your DNS provider, and wait until every record shows **Verified**.
3. Create a **Communication Services** resource (search for "Communication Services" → **Create**), in the same data location.
4. Open the Communication Services resource and go to **Email → Domains → Connect domain**, and pick the domain from step 2.
5. Check the sender address: in the Email Communication Services resource, open the domain → **MailFrom addresses**. The default is `DoNotReply@<your domain>`. This goes in `DIGEST_FROM`.
6. In the Communication Services resource, go to **Settings → Keys** and copy the **Connection string** → `ACS_CONNECTION_STRING` (in your `.env` file).
7. Put the people who should get the email in `DIGEST_TO` (comma-separated).

Use **Send digest now** on the dashboard to test it.

## Install on a server

Before you start you need: Docker with the Compose plugin, a reverse proxy (Caddy or Traefik) already running in Docker, and a DNS name for the dashboard pointing at the server (for example `updates.server1.example.com`).

Pick the example that fits the server:

| Server | Example |
| --- | --- |
| Uses Caddy | [`examples/caddy/compose.yml`](examples/caddy/compose.yml) + [`examples/caddy/Caddyfile`](examples/caddy/Caddyfile) |
| Uses Traefik | [`examples/traefik/compose.yml`](examples/traefik/compose.yml) |

Mailcow isn't a separate kind of server – it runs alongside Caddy or Traefik. If the server runs Mailcow, use the example for its proxy and uncomment the three lines marked `[Mailcow]` in it.

### Steps

1. Find the name of your reverse proxy's Docker network:

   ```sh
   docker network ls
   ```

   The examples assume it's called `proxy`. If yours is different, change `proxy` in the compose file (and, for Traefik, in the `traefik.docker.network` label).

2. Make a folder for the dashboard and go into it. Putting it under `/docker` means it updates itself correctly and shows in hPanel like your other projects:

   ```sh
   mkdir -p /docker/docker-manager
   cd /docker/docker-manager
   ```

3. Copy the right example into this folder as `compose.yml`, and copy [`examples/.env.example`](examples/.env.example) as `.env`.

4. Edit `compose.yml`: set `SERVER_NAME`, `PUBLIC_URL`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and (for the email) `DIGEST_FROM` and `DIGEST_TO`. For Traefik, also set the host name, entry point and certificate resolver in the labels. If the server runs Mailcow, uncomment the lines marked `[Mailcow]`.

5. Edit `.env`: paste in the client secret and (for the email) the ACS connection string, and make a session secret:

   ```sh
   openssl rand -hex 32
   ```

   Paste the result after `SESSION_SECRET=`. Then make the file private:

   ```sh
   chmod 600 .env
   ```

6. **Caddy servers:** add the block from the example `Caddyfile` to your Caddy configuration, with your host name, and reload Caddy.

7. Start it:

   ```sh
   docker compose up -d
   docker compose logs -f
   ```

   Press Ctrl+C to stop watching the logs (the dashboard keeps running).

8. Open `PUBLIC_URL` in your browser and sign in.

> **Note:** `PUBLIC_URL` must match the redirect URI in Azure exactly (same host, `https`), or sign-in fails.

## Configuration reference

All settings are environment variables in the compose file. Secrets go in `.env` next to it.

| Variable | Required | Default | What it's for |
| --- | --- | --- | --- |
| `SERVER_NAME` | Yes | – | Name shown on the page and in the email |
| `PUBLIC_URL` | Yes | – | The dashboard's address, for example `https://updates.server1.example.com` |
| `AZURE_TENANT_ID` | Yes | – | Directory (tenant) ID – the long code, not your domain name |
| `AZURE_CLIENT_ID` | Yes | – | Application (client) ID |
| `AZURE_CLIENT_SECRET` | Yes | – | Client secret **Value** (keep in `.env`) |
| `SESSION_SECRET` | Yes | – | Long random string for signing the sign-in cookie (keep in `.env`). Changing it signs everyone out |
| `ALLOWED_EMAILS` | No | – | Comma-separated **sign-in names** (UPNs) allowed in |
| `ALLOWED_GROUP_ID` | No | – | Object ID of an Entra group allowed in; needs a groups claim |
| `CHECK_INTERVAL_HOURS` | No | `6` | How often to check for updates |
| `COMPOSE_ROOT` | No | `/docker` | Where your compose projects live. Must be mounted at the same path |
| `MAILCOW_DIR` | No | `/opt/mailcow-dockerized` | Mailcow's folder, mounted at the same path. If Mailcow runs on the server but the folder isn't mounted, the Mailcow row says so |
| `ACS_CONNECTION_STRING` | No | – | Azure Communication Services connection string (keep in `.env`). Email is off without it |
| `DIGEST_FROM` | No | – | Sender address set up in Azure Communication Services |
| `DIGEST_TO` | No | – | Comma-separated recipients |
| `DIGEST_SCHEDULE` | No | `0 8 * * MON` | When the email is sent: minute, hour, day of month, month, day of week. Use day names for the weekday (`MON`, `MON-FRI`, `SAT,SUN`); numbers work as in cron (`0` or `7` = Sunday, `1` = Monday) |
| `DIGEST_WHEN_NONE` | No | `true` | Still email when everything is up to date. When `false`, the email is still sent if something couldn't be checked |
| `TZ` | No | `Europe/London` | Time zone for the schedule and the times shown |
| `HEALTH_TIMEOUT_SECONDS` | No | `300` | How long a version update waits for the service to come up healthy before rolling back. It must stay running, without restarting, for 20 seconds |
| `REGISTRY_AUTH_FILE` | No | – | Path to a mounted Docker-style `config.json` with registry logins, only for checking private registries |
| `DATA_DIR` | No | `/data` | Where the database and backups are kept. Leave as it is |

The email is only sent when `ACS_CONNECTION_STRING`, `DIGEST_FROM` and `DIGEST_TO` are all set.

## Updating the dashboard itself

The dashboard checks its own image too, and shows it in the row marked **This dashboard**. Click **Update dashboard**: it starts a short-lived helper container that pulls the new image and restarts the dashboard. The helper's output appears in the job's log. The page will be unavailable for a few seconds; refresh it after a moment.

- After the restart, the job in **History** says **Dashboard restarted – self-update applied**.
- If there was nothing new to pull, the dashboard isn't restarted and the job says **Already up to date – nothing to restart**.
- If the helper fails, the job fails and its log shows the helper's output.

You can also do it by hand:

```sh
cd /docker/docker-manager
docker compose pull
docker compose up -d
```

**For the repository owner:** the image is built by GitHub Actions on every push to `main`/`master` (tagged `latest`) and on version tags like `v1.2.0`. The first time it's published, go to the `dowouk` organisation on GitHub → **Packages → docker-manager → Package settings** and set the visibility to **Public**, so the servers can pull it without signing in.

## Docker Hub limits

You don't need a Docker Hub account – everything works anonymously.

- Checking for a newer build uses `HEAD` requests, which Docker Hub doesn't count towards its pull limit.
- Lists of version tags are cached for 24 hours. Only the newest 1,000 tags of an image are read (Docker Hub doesn't give more without an account), which is plenty for finding newer versions. If a tag list can't be read, the dashboard waits 15 minutes before asking again, and the email lists the image under **Couldn't check**.
- **Updates are real pulls** and do count towards Docker Hub's anonymous limit for your server's IP address. Normal use is well within it.

If the limit is reached, the update stops with the message **"Docker Hub rate limit reached – try again later"**. Nothing is broken – wait a few hours and try again.

## First run checklist

- [ ] You can sign in, and the page shows this server's name.
- [ ] A colleague who is **not** assigned to the app is refused (or try a test account) – this proves **Assignment required** is on.
- [ ] Your compose projects appear grouped by project, not under "Other".
- [ ] None of your `/docker` projects say **Project files not mounted**.
- [ ] Servers that run Mailcow: there is a **Mailcow** row with a version and no error.
- [ ] **Send digest now** delivers an email (if you set up email).
- [ ] On your phone: try the version preview dialog, the live log while an update runs, and the **Show only updates** filter.
- [ ] **hPanel check** – after the first minor/patch update the dashboard does, open that project in hPanel's Docker Manager and confirm it shows the new tag. If hPanel puts the old tag back, stop using the dashboard's version-update button for that project and update it in hPanel instead.

## Troubleshooting

**"AADSTS50011: The redirect URI … does not match"** – the redirect URI in the app registration doesn't exactly match `PUBLIC_URL` + `/auth/callback`. Check the spelling, `https`, and that there's no trailing `/`. Add it under **Authentication → Web → Redirect URIs**.

**"AADSTS50105: … not assigned to a role for the application"** – Assignment required is on (good) but this person isn't assigned. Add them in **Enterprise applications → the app → Users and groups**.

**Sign-in goes round in a loop, or you're refused after signing in** – check that `PUBLIC_URL` starts with `https://` and matches the address in your browser, that `AZURE_TENANT_ID` is the Directory (tenant) ID (not a domain name), and that `ALLOWED_EMAILS` uses sign-in names and `ALLOWED_GROUP_ID` has a groups claim, if you set them.

**"Project files not mounted"** – the project's folder isn't visible inside the dashboard. Projects under `/docker` are covered by the `/docker:/docker` mount. For a project elsewhere, add a mount for its folder at the **same path** (for example `- /opt/myapp:/opt/myapp`) and run `docker compose up -d` again.

**"Docker Hub rate limit reached – try again later"** – see [Docker Hub limits](#docker-hub-limits). Wait and try again.

**"Another update is running – try again when it finishes"** – only one update runs at a time on each server. Wait for the current one to finish (see **History**).

**"Version set by variable – update by hand"** – the image tag in the compose file uses `${…}`. Change the variable yourself (often in the project's `.env`), then run `docker compose up -d` in that project.

**"Project uses an env file that isn't mounted – update by hand"** – the project was started with `--env-file` pointing at a file the dashboard can't see. Mount it at the **same path** (or move it under `/docker`), or update that project by hand.

**"Mailcow folder not mounted (MAILCOW_DIR=…)"** – Mailcow is running but the dashboard can't see its folder. Uncomment the `[Mailcow]` mount (`/opt/mailcow-dockerized:/opt/mailcow-dockerized`) in your compose file, or set `MAILCOW_DIR` to where Mailcow really is.

**Mailcow update fails with local changes or merge conflicts** – the dashboard never tries to fix this. Log in to the server, go to `/opt/mailcow-dockerized`, and sort it out by hand as you would normally (`git status` shows what changed).

**The page shows "502 Bad Gateway"** – the proxy can't reach the dashboard. Check `docker compose logs` for errors (a missing required setting is reported there by name), and that the dashboard and the proxy share the network named in the compose file.

**No email arrives** – press **Send digest now** and read the message at the top of the page. Check the logs, that the domain is connected to the Communication Services resource, and that `DIGEST_FROM` is one of its MailFrom addresses. Look in junk mail.
