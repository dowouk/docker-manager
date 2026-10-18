# Changelog

All notable changes to Docker Manager are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- New dashboard design: a greeting, stat cards with a fleet-health bar, a "Needs attention" section with one card per problem, and a single container table sorted by urgency.
- Filter buttons (All, Updates, Errors, Current) and a search box for the container table. The chosen filter is remembered in the browser.
- Dark and light themes. The page follows the device setting until you choose one with the toggle in the header; the choice is remembered in the browser.
- Account menu on the initials button, with your name, email address and Sign out.
- Favicon and home-screen icon, also shown in the README.
- Geist and Geist Mono fonts, served by the dashboard itself so pages make no outside requests.

### Changed

- Registries that are fronts for Docker Hub, such as `docker.n8n.io`, are now checked on Docker Hub directly. They fetch from Docker Hub with a shared allowance that is nearly always used up, so every check through them failed with a rate-limit error. Checks now use the server's own Docker Hub allowance and login. Docker still pulls from the registry named in the compose file.
- Images that aren't on any registry – built or loaded on the server, or private without credentials – now show a neutral "Can't check" status instead of an error. They are listed last, are left out of the error count and the fleet-health percentage, and are no longer listed in the weekly email.
- Job logs no longer include per-layer pull progress, which could run to hundreds of lines per update. Commands, "Image … Pulled" and the container steps are kept.
- The example compose files keep the dashboard's data in a `data` folder next to the compose file instead of a named Docker volume, so everything for the dashboard is in one folder.
- `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `DIGEST_FROM` and `DIGEST_TO` now come from `.env` in the example compose files, alongside the secrets. `.env.example` lists all seven values.
- History, job log and message pages use the new design.

### Upgrading from 0.1.0

- **Data folder.** To move an existing install to the new `./data` folder, stop the dashboard, copy the contents of the old `dashboard-data` volume into `data/`, change the volume line to `./data:/data`, remove the `volumes:` block at the bottom, and start it again. Your history is kept.
- **`.env`.** If you adopt the new example compose file, add `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `DIGEST_FROM` and `DIGEST_TO` to your `.env` first. Without them, sign-in fails.

## [0.1.0] – 2026-10-18

Initial release.

- Shows whether every container's image is current, by comparing image digests with the registry, and offers newer minor or patch versions of version tags.
- One-click updates: a newer build of the same tag, a minor or patch version (with a preview of the compose-file change, a backup and an automatic rollback if the service doesn't come up healthy), or a whole compose project.
- Updates Mailcow with its own `update.sh`, run on the host exactly as by hand.
- Updates itself through a short-lived helper container.
- Weekly email digest through Azure Communication Services.
- Sign-in with Microsoft Entra ID, restricted to assigned users and optionally to listed users or a group.
- Example compose files for servers behind Caddy or Traefik, with optional Mailcow lines.

[Unreleased]: https://github.com/dowouk/docker-manager/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dowouk/docker-manager/releases/tag/v0.1.0
