<img src="app/static/icon-512.png" alt="" width="72" height="72" align="left">

# Docker Manager

<br clear="left">

A small web page, running on each of your servers, that shows whether your Docker containers are on the newest images – and lets you update them with one click.

- [What it does](#what-it-does)
- [How it decides there's an update](#how-it-decides-theres-an-update)
- [Security](#security)
- [Accounts and sign-in](#accounts-and-sign-in)
- [Set up the Azure app registration](#set-up-the-azure-app-registration-for-microsoft-sign-in)
- [Set up Azure Communication Services email](#set-up-azure-communication-services-email)
- [Install on a server](#install-on-a-server)
- [Settings](#settings)
- [Configuration reference](#configuration-reference)
- [Private images](#private-images)
- [Upgrading from 0.1 or 0.2](#upgrading-from-01-or-02)
- [Monitoring](#monitoring)
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
- Has a **Settings** page for most options, so you don't need to edit files and restart. Changes are kept in **History**.
- Sends a weekly email (Monday 08:00 by default) listing what needs updating. If everything is up to date it still emails, so silence never means "broken".
- Signs people in with a local account (email and password, with two-step sign-in) or with Microsoft 365 / Entra ID, and gives each person a role: Viewer, Operator or Admin. See [Accounts and sign-in](#accounts-and-sign-in).

It never updates anything on its own – every update is a click by a signed-in Operator or Admin.

## How it decides there's an update

For each container it checks two things:

**Newer build** – the tag you use (for example `nginx:latest` or `postgres:16`) now points to a newer image in the registry than the one your container is running. The button runs `docker compose pull` and `docker compose up -d --no-deps` for that service only (the services it depends on are left alone). If several services in the project use the same image, there's also **Update whole project** (not offered for the dashboard's own project). A stopped container shows **Stopped – updating will start it**.

If the newer image has already been pulled but the container was never recreated, it shows **Pulled – not recreated**; the same button recreates it on the newer image.

**Newer version** – the tag is a version number (for example `postgres:16.2` or `1.25.4-alpine`) and a higher version exists. Only tags that look the same are compared – same `v` prefix or not, same ending (`-alpine`, `-bookworm` and so on) and the same number of parts – and test releases (`rc`, `beta`, `alpha`, `dev`, `nightly`, `preview`, `snapshot`) are ignored.

- **Minor or patch** (for example `16.2` → `16.4`): you get a button. You are shown exactly what will change in the compose file first. The file is backed up to the dashboard's `data` folder (`data/backups/<project>/<file>.<date-time>` next to the dashboard's compose file), the tag is changed on that one line only, and the service is pulled and restarted. It then has to stay running (and healthy, if it has a healthcheck) without restarting for 20 seconds, within `HEALTH_TIMEOUT_SECONDS`. If it doesn't, the backup is put back and the old version restarted.
- **Major** (for example `16` → `17`): shown with a warning that it may need a data migration. There's no button – do these by hand.

If the version comes from a variable (for example `image: postgres:${PG_VERSION}`), the dashboard won't edit it and asks you to update by hand.

Updates run `docker compose` the way you would by hand in the project's folder: `${PWD}` in a compose file is the project folder, and if you started the project with `--env-file`, the same files are passed again (they must be visible to the dashboard at the same path – anything under `/docker` is).

**Mailcow** – all Mailcow containers are shown as one **Mailcow** row. The dashboard runs Mailcow's own `./update.sh --check` on the server to see whether Mailcow has an update. **Update Mailcow** runs `./update.sh --force` – the same as running `./update.sh` by hand, without the questions.

Some containers show **Not checkable** – for example images you built yourself, or images pinned by digest (`image@sha256:…`). Containers not started by `docker compose` are shown but can't be updated here.

## Security

Please read this before installing.

- **The dashboard can control Docker on the server.** It needs the Docker socket (`/var/run/docker.sock`) to see and update containers, and access to that socket is the same as root access to the server. This is true on every server, not just the ones that run Mailcow.
- **Sign-in is the protection.** Every page except `/healthz` needs a signed-in account. There is no default password. On a new install the only way in is the one-time setup token, which is kept in the `data` folder and shown in the container log, so whoever can read those is the owner. Roles limit what each person can do, but anyone who can use the Operator or Admin role can update containers, so give those out carefully. If you use Microsoft sign-in, the [Assignment required](#5-only-let-assigned-people-sign-in) step below matters too.
- **On a server that runs Mailcow** the dashboard also runs with `pid: host` and `privileged: true`, so it can run Mailcow's `update.sh` directly on the server, exactly as you would by hand as root. Because the Docker socket already gives root-level control, these options remove friction rather than adding a new kind of risk. Don't think of the other servers as much safer – on all of them, sign-in is what protects you.
- The dashboard is only reachable through your reverse proxy (Caddy or Traefik). The examples have no `ports:` section on purpose – please keep it that way.
- Keep your `.env` file (secrets) private.
- Secrets you save on the Settings page are encrypted – see [Settings](#settings) for what that does and doesn't protect.

## Accounts and sign-in

### First start

On a new install there are no accounts yet. The dashboard makes a one-time **setup token** and puts it in two places: the file `/data/setup-token` (the `data` folder next to your compose file) and the container log (`docker compose logs docker-manager`).

1. Open `<PUBLIC_URL>/setup` (or the dashboard's address plus `/setup`) and enter the token.
2. Create the first Admin: a name, an email and a password. If Microsoft sign-in is already set up (the `AZURE_*` values and `PUBLIC_URL` are in your environment), there is also a **Sign in with Microsoft** button, and the first Admin can be your Microsoft account instead.
3. Setup then closes: `/setup` stops existing and the token file is deleted.

Microsoft sign-in can't skip the token – the token step always comes first. Until setup is finished nobody can sign in with Microsoft.

**Setting up without the web page.** Instead of the token you can give the first Admin in the compose file:

```yaml
    environment:
      PUBLIC_URL: "https://updates.example.com"
      INITIAL_ADMIN_EMAIL: "you@example.com"
      INITIAL_ADMIN_PASSWORD_FILE: "/run/secrets/dm-admin-password"
```

`INITIAL_ADMIN_PASSWORD_FILE` is a file mounted into the container that holds the password (the file must be 4 KiB or smaller, and the password must pass the rules below). `PUBLIC_URL` must also be set. The Admin is created on first start and must choose a new password at first sign-in. If anything about these settings is wrong, the log says so and the dashboard falls back to the setup token. These two variables are only used while the dashboard has no users.

### Local accounts

- Passwords need **12 or more characters**. There are no other composition rules, but passwords on a list of very common ones are refused.
- **Lockouts:** 5 wrong attempts for one account, or 20 from one address, within 15 minutes locks that account (or blocks that address) for 15 minutes. An unknown email and a wrong password look exactly the same. The address limit is best effort, because the address comes from your proxy; the account lock is the real protection.
- **Two-step sign-in** (an authenticator app) is required for local Admins by default. You are asked to set it up at your next sign-in. You also get **10 single-use recovery codes**: store them somewhere safe. Admins can turn the requirement off on the Settings page (while it is on, Admins can't switch it off for themselves), and anyone else can turn two-step sign-in on for their own account from **Account**.
- People change their own password, set up two-step sign-in, see their recovery codes and sign out of every device on the **Account** page.
- Admins add people on the **Users** page by sending an **invite link** (valid for 72 hours) or by giving a **one-time password**; either way the person chooses their own password first. Two-step sign-in only applies to local accounts: Microsoft accounts use Microsoft's own sign-in checks.

### Roles

| Role | Can do |
| --- | --- |
| **Viewer** | See the dashboard, the History list of updates (without logs) and their own Account page |
| **Operator** | Everything a Viewer can, plus Check now, all container, project and version updates and previews, and job logs and live logs |
| **Admin** | Everything an Operator can, plus Settings, Users, the History **Changes** tab, Send digest now, the Mailcow update and the dashboard's own update |

There is also a **Waiting for approval** state with no access at all.

**Default role for new Microsoft users.** On the Settings page (Sign-in, "Role for new Microsoft sign-ins") you choose what someone gets the first time they sign in with Microsoft and nothing else decides their role: **Waiting for approval**, **Viewer** (the default) or **Operator**. Admins approve people on the **Users** page by giving them a role.

**Pinned or "From Entra".** On the Users page a role is marked **Pinned** when an Admin set it by hand (approving someone, or changing their role). Pinned roles are never changed by Microsoft sign-in. A role marked **From Entra** is worked out again at every Microsoft sign-in from the person's app roles and groups (the highest one wins; no match gives the default role).

**There is always an Admin.** The last active Admin can't be demoted, disabled or deleted, and a Microsoft role recompute never demotes them – the sign-in keeps them as Admin and logs a warning.

### Microsoft sign-in

Microsoft sign-in is set up on the **Settings** page, not in the compose file:

1. Create the app registration (see [Set up the Azure app registration](#set-up-the-azure-app-registration-for-microsoft-sign-in)).
2. On **Settings → Sign-in**, enter the **Tenant ID** (a GUID, not your domain name), the **Client ID** and the **Client secret**, and save.
3. The dashboard's address must be known: `PUBLIC_URL` in your compose file, or the dashboard address under **General** on the Settings page. The page then shows the **redirect URI** (the address plus `/auth/callback`) – add that to the app registration as a Web redirect URI. The address is never taken from the browser's request, so without it Microsoft sign-in stays off.

The Sign-in section also has: whether local sign-in stays on (it can't be switched off unless Microsoft sign-in works, so you can't lock yourself out), whether to hide the local form from people who aren't Admins, whether local Admins need two-step sign-in, and the default role. These four settings exist only on the Settings page – environment variables don't set them.

**Matching is by sign-in name.** People are matched by their **sign-in name** (the user principal name, `preferred_username`), not their email address – email claims can be unverified for guest accounts. Email is only shown. If you add someone on the Users page before they first sign in with Microsoft, enter their sign-in name so the two join up.

### Roles from Microsoft: app roles and groups

You can give roles from Entra, so people are promoted and demoted without visiting the Users page. Use **app roles** (recommended) and/or **group mappings**.

**App roles.** In the app registration, go to **App roles → Create app role** and add three, with these exact values:

| Display name | Allowed member types | Value |
| --- | --- | --- |
| Docker Manager Admin | Users/Groups | `DockerManager.Admin` |
| Docker Manager Operator | Users/Groups | `DockerManager.Operator` |
| Docker Manager Viewer | Users/Groups | `DockerManager.Viewer` |

Give each a description and make sure **Do you want to enable this app role?** is ticked. Then go to **Enterprise applications** → the app → **Users and groups → Add user/group** and assign each person (or group) with the role you want them to have.

**Group mappings.** On **Settings → Sign-in** (Roles from Microsoft groups), enter a group's **Object ID** and a role. This needs the app to send group membership in the sign-in token: in the app registration, go to **Token configuration → Add groups claim**, tick **Security groups** (or **Groups assigned to the application**), and **Save**.

If someone has several, the **highest** role wins.

**Group overage – use app roles if you can.** When a person is in too many groups, Entra leaves the groups out of the token. In that case the dashboard can't tell what they should have, so an existing person who isn't pinned **keeps their current role** (and a warning is logged). The result is that someone removed from a group can keep their role until their token carries groups again or an Admin changes it. App roles don't have this problem.

### Lost access: `reset-admin`

If nobody can sign in, run this on the server:

```sh
docker exec docker-manager docker-manager reset-admin you@example.com
```

It creates the local Admin (or resets the existing one), and prints a **one-time password**. It also clears that account's failed attempts and lock, turns off its two-step sign-in so they enrol again, ends all of their sessions, and turns **local sign-in back on** if it had been switched off – restart the container (`docker compose restart`) for that last part to apply. The person must choose a new password at their next sign-in. The change is recorded in History.

## Set up the Azure app registration for Microsoft sign-in

This is only needed if you want people to sign in with Microsoft; local accounts work without it. You need one app registration. You can use the same one for all your servers – just add a redirect URI for each.

### 1. Register the app

1. Sign in to the [Microsoft Entra admin centre](https://entra.microsoft.com).
2. Go to **Identity → Applications → App registrations → New registration**.
3. **Name:** `Docker Manager` (anything you like).
4. **Supported account types:** **Accounts in this organisational directory only (Single tenant)**.
5. **Redirect URI:** choose **Web**, and enter `https://<your-host>/auth/callback` – for example `https://updates.server1.example.com/auth/callback`.
6. Click **Register**.

### 2. Copy the IDs

On the app's **Overview** page, copy:

- **Application (client) ID** → the **Client ID** on the Settings page (or `AZURE_CLIENT_ID`)
- **Directory (tenant) ID** → the **Tenant ID** (or `AZURE_TENANT_ID`) – the long code like `1a2b3c4d-…`, not your domain name

### 3. Add each server's address

For each extra server, go to **Authentication → Web → Redirect URIs → Add URI** and add `https://<that server's host>/auth/callback`. Click **Save**.

### 4. Create a client secret

1. Go to **Certificates & secrets → Client secrets → New client secret**.
2. Give it a description and choose how long it lasts (put a reminder in your calendar to renew it before it expires).
3. Click **Add**, then copy the **Value** straight away (it's only shown once) → the **Client secret** on the Settings page (or `AZURE_CLIENT_SECRET`). Don't copy the "Secret ID" – that's a different thing.

### 5. Only let assigned people sign in

> **Important:** without this step, **anyone in your Microsoft 365 organisation could sign in**. They would get the default role (Viewer, unless you changed it), so set the default role to **Waiting for approval** if you don't want that.

1. Go to **Identity → Applications → Enterprise applications** and open the app with the same name.
2. Go to **Properties**, set **Assignment required?** to **Yes**, and click **Save**.
3. Go to **Users and groups → Add user/group**, choose yourself (and anyone else who should have access), and click **Assign**.

### 6. Optional: app roles and groups

See [Roles from Microsoft: app roles and groups](#roles-from-microsoft-app-roles-and-groups) above. The old `ALLOWED_EMAILS` and `ALLOWED_GROUP_ID` settings are gone from day-to-day use; see [Upgrading from 0.1 or 0.2](#upgrading-from-01-or-02).

## Set up Azure Communication Services email

This is only needed for the weekly email. Skip it if you don't want emails. You can enter the values below either in your `.env` file or on the dashboard's [Settings page](#settings).

1. In the [Azure portal](https://portal.azure.com), create an **Email Communication Services** resource (search for "Email Communication Services" → **Create**). Choose a resource group and a data location (for example **UK** or **Europe**).
2. Open it and go to **Provision domains**. Either:
   - **Add an Azure managed domain** – quickest; it gives you an address like `DoNotReply@xxxx.azurecomm.net`, or
   - **Add a custom domain** (for example `example.com`), then add the TXT, SPF and DKIM records it shows you at your DNS provider, and wait until every record shows **Verified**.
3. Create a **Communication Services** resource (search for "Communication Services" → **Create**), in the same data location.
4. Open the Communication Services resource and go to **Email → Domains → Connect domain**, and pick the domain from step 2.
5. Check the sender address: in the Email Communication Services resource, open the domain → **MailFrom addresses**. The default is `DoNotReply@<your domain>`. This is the sender (`DIGEST_FROM`).
6. In the Communication Services resource, go to **Settings → Keys** and copy the **Connection string** → `ACS_CONNECTION_STRING`. Enter it on the Settings page (it is encrypted when saved), or put it in your `.env` file.
7. Put the people who should get the email in the recipients (`DIGEST_TO`, comma-separated).

Use **Send test email** on the Settings page to check it works, or **Send digest now** on the dashboard.

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

   Everything for the dashboard then lives in this folder: `compose.yml`, `.env`, and a `data` folder that is created on first start and holds the database and compose-file backups. Back up this one folder.

4. Edit `compose.yml`: set `PUBLIC_URL` (the address people will open). For Traefik, also set the host name, entry point and certificate resolver in the labels. If the server runs Mailcow, uncomment the lines marked `[Mailcow]`.

5. `.env` is optional now. Microsoft sign-in and the email settings can be set up on the [Settings page](#settings) after signing in; fill in the `AZURE_*` lines only if you would rather keep Microsoft sign-in in the file. Make the file private:

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

8. Find the setup token (`cat data/setup-token`, or in the log), open `PUBLIC_URL/setup` in your browser, enter the token and create the first Admin – see [First start](#first-start). Then open **Settings** to set the server name, Microsoft sign-in, email and anything else.

> **Note:** if you use Microsoft sign-in, `PUBLIC_URL` plus `/auth/callback` must match the redirect URI in Azure exactly (same host, `https`), or sign-in fails.

## Settings

Open **Settings** from the top of the dashboard. Most options can be changed there, with no file editing or restart:

- **General** – server name, time zone and the dashboard's address.
- **Sign-in** – Microsoft sign-in (tenant ID, client ID, client secret and the redirect URI to copy), roles from Microsoft groups, local sign-in, two-step sign-in for Admins and the default role. See [Accounts and sign-in](#accounts-and-sign-in).
- **Checking** – how often to check for updates, how long a version update waits for the service to come up healthy, and the Mailcow folder.
- **Email** – the Azure Communication Services connection string, sender, recipients, the schedule, and whether to email when everything is up to date. **Send test email** sends a message straight away so you can check it works.
- **Registry logins** – logins for private images (see [Private images](#private-images)). Each one is checked against the registry before it is saved.

Each section is saved on its own. If one value in a section is invalid (for example a schedule that isn't valid, or an unknown time zone) nothing in that section is saved, and the form shows the problem with your other entries still in place. Changes appear in **History → Changes** – the old and new values, but never secrets.

**Environment variables still win.** Every setting that could be set with an environment variable in 0.1 still can, and an environment variable always beats the Settings page. A field set that way is shown read-only as "Set by the server's environment (`NAME`)". To manage it on the page instead, remove the variable from your compose file or `.env` and run `docker compose up -d`.

**Secrets are encrypted.** The Microsoft client secret, the connection string and registry passwords or tokens that you save on the page are encrypted in the database, and are never shown again or sent back to your browser. To keep one as it is, leave its field blank when you save. The key is created automatically the first time the dashboard starts, as `/data/secret.key` (in your `data` folder).

What this does and doesn't do:

- It protects a copy of the database file on its own – for example one that ends up in a backup or is sent to someone for help.
- It does **not** protect against someone who has all of the `data` folder, because the key is in it.
- For stronger protection, keep the key somewhere else: put `secret.key` in a separate folder, mount that folder in the container, and point `SECRET_KEY_FILE` at it (for example `SECRET_KEY_FILE: "/run/dm-key/secret.key"`). Then back up the database and the key separately.
- Back the key up. If the key file is missing, a new one is created when the dashboard starts, so anything saved with the old key can't be read: the dashboard still starts, the fields say "The saved value can't be read – enter it again", and you enter them again. If the file is there but can't be used (it is not 32 bytes long, or can't be read), the dashboard still starts and logs one error, but it never overwrites the file; saved secrets can't be read and new ones can't be saved until you restore the original key or fix the file.
- The folder for `SECRET_KEY_FILE` must be writable by the dashboard. When moving the key there from the default place, **move the existing `secret.key` into that folder first** – if you only set the variable, a new key is created and the secrets you saved can no longer be read.

**Upgrading from 0.1.** If you copy the new example compose file over your old one, your `AZURE_*` values (now commented out in the example) and the `ACS_CONNECTION_STRING`, `DIGEST_FROM` and `DIGEST_TO` lines (and `DIGEST_SCHEDULE`, `DIGEST_WHEN_NONE`) that 0.1 passed through are dropped. Either keep those lines in your compose file, or enter the email settings on the Settings page. A variable that is set but empty (for example `ACS_CONNECTION_STRING=` to switch email off) no longer locks the Email section; only a variable with a value does.

Nothing is needed in the environment to start the dashboard any more: `PUBLIC_URL` and the `AZURE_*` values are optional because they can be set on the Settings page. Only `DATA_DIR`, `COMPOSE_ROOT` and `SECRET_KEY_FILE` are set with environment variables alone. The four sign-in settings (local sign-in, hiding local sign-in, two-step sign-in for Admins and the default role) are on the Settings page only.

## Configuration reference

Settings can be environment variables in the compose file, and many can also be changed on the [Settings page](#settings) – the **Settings page** column shows which. If you keep the Entra app registration in the environment instead of the Settings page, put its secret in `.env` next to the compose file; the examples read it from there. If a variable is set, it wins over the Settings page and the field shows as locked there.

| Variable | Required | Default | Settings page | What it's for |
| --- | --- | --- | --- | --- |
| `SERVER_NAME` | No | `Docker Manager` | Yes | Name shown on the page and in the email |
| `PUBLIC_URL` | No | – | Yes | The dashboard's address, for example `https://updates.server1.example.com`. Needed for Microsoft sign-in (the redirect URI is built from it) and for `INITIAL_ADMIN_EMAIL` |
| `AZURE_TENANT_ID` | No | – | Yes | Directory (tenant) ID – the long code, not your domain name. Optional: set it on the Settings page instead |
| `AZURE_CLIENT_ID` | No | – | Yes | Application (client) ID. Optional: or set on the Settings page |
| `AZURE_CLIENT_SECRET` | No | – | Yes | Client secret **Value** (keep in `.env`). Optional: or set on the Settings page |
| `SESSION_SECRET` | No | – | – | No longer needed – ignored if set, so 0.1 `.env` files still work |
| `ALLOWED_EMAILS` | No | – | No | 0.1/0.2 only, **read once when upgrading** and ignored afterwards: become pre-created Admins. See [Upgrading](#upgrading-from-01-or-02) |
| `ALLOWED_GROUP_ID` | No | – | No | 0.1/0.2 only, **read once when upgrading** and ignored afterwards: becomes a group mapping to Admin |
| `INITIAL_ADMIN_EMAIL` | No | – | No | Creates the first Admin without the setup token. Needs `INITIAL_ADMIN_PASSWORD_FILE` and `PUBLIC_URL` |
| `INITIAL_ADMIN_PASSWORD_FILE` | No | – | No | Path to a file (4 KiB or smaller) with that Admin's password; they must change it at first sign-in |
| `CHECK_INTERVAL_HOURS` | No | `6` | Yes | How often to check for updates |
| `COMPOSE_ROOT` | No | `/docker` | No | Where your compose projects live. Must be mounted at the same path |
| `MAILCOW_DIR` | No | `/opt/mailcow-dockerized` | Yes | Mailcow's folder, mounted at the same path. If Mailcow runs on the server but the folder isn't mounted, the Mailcow row says so |
| `ACS_CONNECTION_STRING` | No | – | Yes | Azure Communication Services connection string (keep in `.env`). Email is off without it |
| `DIGEST_FROM` | No | – | Yes | Sender address set up in Azure Communication Services |
| `DIGEST_TO` | No | – | Yes | Comma-separated recipients |
| `DIGEST_SCHEDULE` | No | `0 8 * * MON` | Yes | When the email is sent: minute, hour, day of month, month, day of week. Use day names for the weekday (`MON`, `MON-FRI`, `SAT,SUN`); numbers work as in cron (`0` or `7` = Sunday, `1` = Monday) |
| `DIGEST_WHEN_NONE` | No | `true` | Yes | Still email when everything is up to date. When `false`, the email is still sent if something couldn't be checked |
| `TZ` | No | `Europe/London` | Yes | Time zone for the schedule and the times shown |
| `HEALTH_TIMEOUT_SECONDS` | No | `300` | Yes | How long a version update waits for the service to come up healthy before rolling back. It must stay running, without restarting, for 20 seconds |
| `REGISTRY_AUTH_FILE` | No | – | Logins can be added there instead | Path to a mounted Docker `config.json` with registry logins, for private images. Used for checks, and – when the file is named `config.json` – for pulls too. See [Private images](#private-images) |
| `DATA_DIR` | No | `/data` | No | Where the database and backups are kept inside the container. Leave as it is; the examples map it to the `data` folder next to the compose file |
| `SECRET_KEY_FILE` | No | `<DATA_DIR>/secret.key` | No | Where the key that encrypts saved secrets is kept. Created on first start; leave as it is |

The email is only sent when the connection string, sender and recipients are all set – by `ACS_CONNECTION_STRING`, `DIGEST_FROM` and `DIGEST_TO`, or on the Settings page.

`SECRET_KEY_FILE`, `DATA_DIR`, `COMPOSE_ROOT`, `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD_FILE` are only ever set with environment variables.

## Private images

For images in a private registry (for example a private package on `ghcr.io`), add a login for it on the dashboard's **Settings** page, under **Registry logins**.

1. Create a **read-only token** at the registry rather than using an account password. For GitHub, use a personal access token with only the `read:packages` permission. Docker Hub has read-only access tokens too.
2. On **Settings → Registry logins**, enter the registry host (for example `ghcr.io`), the username and the token, and save. The dashboard checks the login against the registry first, and doesn't save it if it is refused.
3. Press **Check now**.

Logins saved this way are encrypted (see [Settings](#settings)) and handed to Docker by a small credential helper when it needs them. They are never written to a file in plain text. They are used both to check the image and to pull it when you update. A Docker Hub login also raises Docker Hub's limits (see [Docker Hub limits](#docker-hub-limits)).

### Using `REGISTRY_AUTH_FILE` instead

This still works exactly as before, and the logins in the file keep working alongside any you add on the Settings page. But the file is your own plain-text `config.json`, so moving those logins to the Settings page is recommended.

1. Log in on the server as root (with a read-only token):

   ```sh
   docker login ghcr.io
   ```

2. In the dashboard's `compose.yml`, add the setting and mount the folder (read-only):

   ```yaml
       environment:
         REGISTRY_AUTH_FILE: "/run/docker-auth/config.json"
       volumes:
         - /root/.docker:/run/docker-auth:ro
   ```

   Mount the folder, not just `config.json`: `docker login` replaces the file, and a mounted single file would go on showing the old one.

3. Run `docker compose up -d`, then press **Check now**.

The login must be stored in `config.json` itself. If `docker login` says it used a credential helper (`credsStore`), the dashboard can't read it.

## Upgrading from 0.1 or 0.2

Pull the new image and run `docker compose up -d`. **Keep your existing compose file and `.env` for the upgrade** – don't replace them with the new examples. You don't need to change either. If you do switch to the new example files, carry your `AZURE_*` values over (or enter them on the Settings page) and keep `ALLOWED_EMAILS` and `ALLOWED_GROUP_ID` in place until the first 0.3 start has run, because that is when they are read.

- **There is no setup token** on an install that already has a database – you are never sent to `/setup`.
- **Everyone signs in again once.** Sessions changed, so old ones are no longer recognised.
- **`ALLOWED_EMAILS`** – each sign-in name becomes a pre-created, pinned **Admin** (they join up with the person at their next Microsoft sign-in).
- **`ALLOWED_GROUP_ID`** – becomes a group mapping that gives members the **Admin** role (see Settings → Sign-in). This needs the groups claim you set up in 0.1. If the value isn't a group Object ID (a GUID), it is skipped with an error in the log; add the group on the Settings page, or run `reset-admin` to get in.
- **With either one set**, the default role becomes **Waiting for approval**, because 0.1 refused everyone else. New people wait until an Admin approves them on the **Users** page.
- **With neither set**, 0.1 let everyone who could sign in do everything, so the default role starts as **Admin (as in 0.1)** and a banner says so on every page for Admins. Everyone who signs in stays an Admin until you change the default role on the Settings page and set roles on the Users page. Do this soon.
- `ALLOWED_EMAILS` and `ALLOWED_GROUP_ID` are read **once**, at the upgrade, and ignored after that – removing them from your compose file changes nothing, and adding them later does nothing either. Manage access on the Users and Settings pages.
- Your existing `AZURE_*` and `PUBLIC_URL` values keep working. They show as locked on the Settings page until you remove them from the compose file or `.env`.

## Monitoring

`/healthz` is public (no sign-in) and returns a small JSON status (`{"status": "ok"}` with **200** when the dashboard and its database are working, or `"unavailable"` with **503**). Nothing else is revealed. The Docker image has a built-in health check that uses it, so `docker ps` shows `healthy` or `unhealthy`.

- **Uptime Kuma:** add an **HTTP(s)** monitor for `https://updates.server1.example.com/healthz`, accepted status code `200-299`.
- **Caddy or Traefik:** you don't need a health check in the proxy for this; use the one above if you want one.

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

- [ ] You can sign in, and the page shows this server's name. `/setup` now says not found, and `data/setup-token` is gone.
- [ ] Microsoft sign-in only: a colleague who is **not** assigned to the app is refused (or try a test account) – this proves **Assignment required** is on – and one who is gets the role you expect.
- [ ] Your compose projects appear grouped by project, not under "Other".
- [ ] None of your `/docker` projects say **Project files not mounted**.
- [ ] Servers that run Mailcow: there is a **Mailcow** row with a version and no error.
- [ ] **Send test email** on the Settings page (or **Send digest now**) delivers an email, if you set up email.
- [ ] Upgrading from 0.1 or 0.2: everyone is asked to sign in again once. That is expected. If a banner says everyone is an Admin, set the default role and roles on the Users page.
- [ ] On your phone: try the version preview dialog, the live log while an update runs, and the **Show only updates** filter.
- [ ] **hPanel check** – after the first minor/patch update the dashboard does, open that project in hPanel's Docker Manager and confirm it shows the new tag. If hPanel puts the old tag back, stop using the dashboard's version-update button for that project and update it in hPanel instead.

## Troubleshooting

**"AADSTS50011: The redirect URI … does not match"** – the redirect URI in the app registration doesn't exactly match `PUBLIC_URL` + `/auth/callback`. Check the spelling, `https`, and that there's no trailing `/`. Add it under **Authentication → Web → Redirect URIs**.

**"AADSTS50105: … not assigned to a role for the application"** – Assignment required is on (good) but this person isn't assigned. Add them in **Enterprise applications → the app → Users and groups**.

**Sign-in goes round in a loop, or you're refused after signing in** – check that `PUBLIC_URL` starts with `https://` and matches the address in your browser, and that the tenant ID is the Directory (tenant) ID (not a domain name). If you see "Waiting for approval", an Admin needs to approve you on the Users page.

**Nobody can sign in, or there is no Admin left** – run `reset-admin` (see [Lost access](#lost-access-reset-admin)).

**"Too many attempts – try again in 15 minutes."** – five wrong passwords for an account, or twenty from one address. Wait 15 minutes, or run `reset-admin` for that email, which clears the account's failures and lock (an address block from the same IP still lasts 15 minutes).

**Microsoft sign-in isn't offered** – it needs the tenant ID, client ID and client secret **and** the dashboard's address (`PUBLIC_URL` or the Settings page).

**Someone's role isn't what the group says** – roles you set by hand are pinned, and when Entra leaves groups out of the token (see group overage) people keep their current role. Use app roles.

**"Project files not mounted"** – the project's folder isn't visible inside the dashboard. Projects under `/docker` are covered by the `/docker:/docker` mount. For a project elsewhere, add a mount for its folder at the **same path** (for example `- /opt/myapp:/opt/myapp`) and run `docker compose up -d` again.

**"Docker Hub rate limit reached – try again later"** – see [Docker Hub limits](#docker-hub-limits). Wait and try again.

**"Another update is running – try again when it finishes"** – only one update runs at a time on each server. Wait for the current one to finish (see **History**).

**"Version set by variable – update by hand"** – the image tag in the compose file uses `${…}`. Change the variable yourself (often in the project's `.env`), then run `docker compose up -d` in that project.

**"Project uses an env file that isn't mounted – update by hand"** – the project was started with `--env-file` pointing at a file the dashboard can't see. Mount it at the **same path** (or move it under `/docker`), or update that project by hand.

**"Mailcow folder not mounted (MAILCOW_DIR=…)"** – Mailcow is running but the dashboard can't see its folder. Uncomment the `[Mailcow]` mount (`/opt/mailcow-dockerized:/opt/mailcow-dockerized`) in your compose file, or set `MAILCOW_DIR` to where Mailcow really is.

**Mailcow update fails with local changes or merge conflicts** – the dashboard never tries to fix this. Log in to the server, go to `/opt/mailcow-dockerized`, and sort it out by hand as you would normally (`git status` shows what changed).

**The page shows "502 Bad Gateway"** – the proxy can't reach the dashboard. Check `docker compose logs` for errors (a missing required setting is reported there by name), and that the dashboard and the proxy share the network named in the compose file.

**No email arrives** – press **Send test email** on the Settings page and read the message. This is the way to check email: for safety, a failed send is logged with the type of error only, never its text (which could contain your connection string), so the logs say little. Check that the domain is connected to the Communication Services resource, and that the sender (`DIGEST_FROM`) is one of its MailFrom addresses. Look in junk mail.

**Everyone was signed out after upgrading to 0.2 or 0.3** – expected, and it happens once. Sessions are now kept on the server instead of in a signed cookie, so old cookies are no longer recognised. Just sign in again. `SESSION_SECRET` is no longer needed and is ignored if it is still set.

**A field on the Settings page is read-only and says "Set by the server's environment"** – that setting is also set as an environment variable (in your compose file or `.env`), and the variable always wins. Remove it there and run `docker compose up -d` to manage it on the page.

**"The saved value can't be read – enter it again"** – the key that encrypts saved secrets (`/data/secret.key`, or `SECRET_KEY_FILE`) is missing or has been replaced. The dashboard still runs; enter the secret again and save. Restore the original key file if you have it.
