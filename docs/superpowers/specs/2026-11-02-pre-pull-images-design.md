# Download updates ahead and tidy unused images – design

Date: 2026-11-02
Status: draft, for the owner's approval

## Why

Updating a container is slow mostly because of the image download. The owner wants the download done in the background as soon as a check finds an update, so that clicking **Update** only has to recreate the container. Disk space is plentiful, so leftover images are not a worry in themselves – but the owner wants to see what can be deleted and clear it with one button.

## Goals

1. After every check (scheduled or **Check now**), download the images for the updates it found, in the background.
2. Show on each container row whether its update is downloaded, downloading, or failed to download.
3. A new **Images** page listing every unused image on the server, with size, how long it's been unused and the total that could be freed, a **Delete** button per image and a **Tidy all unused** button.
4. A **Needs attention** card on the dashboard when unused images pass a size set in Settings.
5. One line in the weekly digest with the count and size of unused images.

## Non-goals

- Applying updates automatically. Every update stays a click by an Operator or Admin – the README's promise ("It never updates anything on its own") stays true.
- Automatic or scheduled tidying. Deleting is always a click.
- Build cache, volumes, networks or stopped containers. Only images.
- Pre-downloading major versions (they have no Update button), Mailcow (its own `update.sh` handles it), or skipped, snoozed or unwatched updates.

## Anti-goals – it fails if…

- A background download ever blocks or delays an update, restart, config change or other job someone clicked.
- **Tidy all unused** deletes an image that's waiting to be applied, so the next Update has to download it again.
- Tidying deletes an image a container uses (running or stopped).
- The self-update of the dashboard stops working.

## How it behaves (owner's decisions)

- **Automatic**, after each check.
- **Both kinds of update**: a newer build of the tag in use, and a new minor/patch version.
- **"Unused" means every image on the server that no container uses**, running or stopped – not only images the dashboard made redundant. The owner accepts that this includes images kept on purpose for projects that are down (they'd be downloaded again next time they're started).
- **Placement: option C** from the mockups – a new **Images** page in the menu (between Layout and Users), plus a dashboard card.
- Disk space is not a concern.

## Downloading ahead

### What gets downloaded

After a check completes, for each container whose update is on offer (not skipped, snoozed or unwatched):

- **Newer build** (`Status.NEWER_BUILD`): pull the tag the service uses, the same pull the Update button runs today (`docker compose pull <service>` in the project folder, with the project's env files). The tag then points at the new image locally, and the next check reports the container as **Pulled – not recreated** (`Status.PULLED_NOT_RECREATED`), which already exists. Update still runs its pull first (see "Update still checks the registry" below), but with the layers already on the server it takes seconds.
- **Minor/patch version**: pull `<repository>:<new tag>` (the version the Update button would move to) with `docker pull`, without touching the compose file. If the service sets `platform:`, pass the same platform. The compose edit, backup, pull (now near instant) and health check all still happen when Update is clicked.

Left out: major versions, Mailcow, containers not started by compose, versions set by a variable (`${PG_VERSION}`), and **the dashboard's own image** (see below).

### How it runs

- Pulls run as a background task after the check, **outside the `JobRunner`** – the runner allows one job at a time, and a click must never get "busy" because of a pre-download.
- One pull at a time, in turn. If a pull for the same image is already running or that image is already present locally, skip it.
- Credentials come the same way as Update pulls today (`subprocess_env()`, the credential helper / `DOCKER_CONFIG`).
- If someone clicks Update for a service while its pre-download is running, the Update goes ahead normally; Docker shares the in-flight layer downloads.
- Failures (registry down, rate limit, auth) are logged in the app log and shown on the row; the next check tries again. No History entry for pre-downloads – they'd swamp the list.
- On shutdown, a running pre-download is simply abandoned.

### Shown on the container row

As in the mockup:

- **"16.4 ready"** / **"Newer build ready"** (green badge), with "Downloaded <time> ago".
- **"<version> – downloading…"** while in progress.
- **"Couldn't download ahead – will try again at the next check"** as small text when the last attempt failed. The Update button works exactly as today.

For newer builds, the existing **Pulled – not recreated** label is replaced by **Newer build ready** (README updated to match).

### Update still checks the registry (my call)

`update_build` and `update_version` always run `docker compose pull` before starting the service. That stays as it is: with the image already downloaded, the pull only checks the registry and finishes in seconds, and it also picks up a build newer still if one appeared since. The catch is that if the registry is unreachable or rate-limiting at that moment, Update fails as it does today – so the badge doesn't promise "only restarts it". Skipping the pull when the image is already present could come later.

### Shared tags (my call)

A tag is shared by every container on the server that uses it, but watch rules, skips and snoozes are per container. Pulling a newer build moves the tag for all of them. So a newer build is only downloaded ahead when **every** container using that exact tag (including Mailcow containers and containers not started by compose) is watched and doesn't have that update skipped or snoozed. Otherwise it's left until someone clicks Update, as today. Version downloads aren't affected – they pull a new tag no container uses yet.

### Running alongside clicked jobs (my call)

"Never blocks or delays a clicked job" means the job runner never refuses or queues a job because of a download. A download already in progress keeps going alongside a clicked job and shares the bandwidth; there's no pause/resume. Downloads run one at a time, so at most one competes.

### Setting

Settings → Checking: **Download updates ahead** (yes/no), default **yes**.

### The dashboard's own image (builder's note, my call)

Excluded. The self-update helper decides whether to restart by whether the pull found something new ("Already up to date – nothing to restart"); a pre-pulled tag would make it never restart. Pre-downloading the dashboard could be added later by changing the helper to compare the running image with the tag's image instead.

### Side effect to document (my call)

Once a newer build has been pulled, anything that runs `docker compose up -d` for that service will start it on the new image – by hand on the server, or the dashboard's **Save and apply** in Edit config. This is how Docker behaves with any pulled tag, and the existing "Pulled – not recreated" state already has it. The README should say so in the Newer build section. Version updates aren't affected (the compose file still names the old tag until Update is clicked).

## Images page

- New menu item **Images** between Layout and Users. Viewers can see it; only Operators and Admins see the Delete and Tidy buttons.
- **Unused images**: every image no container uses (running or stopped), including untagged ones (shown as "untagged" with a short ID). Columns: image (all its tags), size, unused since. Sortable by size and by unused since. Heading shows count and total size, e.g. "Unused images · 9 · 6.4 GB".
- **Waiting to be applied**: a separate, smaller list of images downloaded ahead for an update that's still on offer. No Delete button, never tidied. Protection is worked out from the latest check result, not stored: an image is protected while it's the downloaded image for a newer build or version update still on offer (not skipped/snoozed). Once the update is applied, skipped or overtaken by something newer, the image moves to Unused on the next check.
- **Delete** on each unused row, and **Tidy all unused** for the lot. Both ask to confirm (the existing confirm dialog), showing what will be freed.
- Deleting runs as a job (kind e.g. "tidy images") so it's in **History** with who, which images and how much space was freed. Deleting is by image ID without force; if Docker refuses one (a container started using it in the meantime), that image is skipped, the job notes it, and the rest go ahead.

### "Unused since"

Docker doesn't record when an image stopped being used. The dashboard records the first time it saw each image unused (in its store) and clears the record when the image is used again or deleted. Images already unused at the first scan after this ships show "before <date of that scan>", since the real time isn't known. (My call; the builder may refine the wording.)

The list is worked out when the page loads and after each check, from the Docker API (images and all containers); it doesn't need the network.

## Needs attention card

- Shows when total unused image size is over **Settings → Checking → Unused images warning (GB)**, default **10**. **0** turns the card off.
- Text: "Unused images – 9 images, 6.4 GB could be freed." with a **Review** button to the Images page.
- Follows the dashboard's existing attention-card styling. It isn't a container, so the filter chips don't apply to it (like the Mailcow card when it's not an update).

## Weekly digest

One line, when there are any unused images: "9 unused images, 6.4 GB" with a link to the Images page. Not shown when there are none. On its own it doesn't count as something to report – a digest that would otherwise be skipped (nothing to report and "send when there's nothing" off) is still skipped. (My call.) "Updates found" messages are unchanged.

## Roles and audit

- Viewer: see the Images page and the card.
- Operator / Admin: Delete and Tidy all unused.
- Every delete is a job in History. The two new settings are audited like other Settings changes.

## Docs and release

- README: Download ahead (and the `up -d` side effect), the Images page, the card, the two settings, the digest line, Docker Hub limits (pre-downloads are real pulls; one that's overtaken before use costs a pull for nothing – normal use is still well within the limit).
- CHANGELOG entry under `[Unreleased]` via `dowo:changelog`; no version bump in this branch.
- Unit tests alongside the code; the full suite passes before merge.

## Left to the builder

- Module layout (e.g. a new `app/prefetch.py` and `app/images.py`), store schema for download state and "unused since", and how the row badge reads download state.
- Exact sort defaults and empty-state text on the Images page.
- Whether download state survives a restart (it can be re-derived: the next check sees the image locally).
