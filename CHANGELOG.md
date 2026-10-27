# Change Log

All notable changes to Docker Manager are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.5.7] - 2026-10-27

### Fixed

- Update now recreates the container on the newly pulled image. With Docker's containerd image store, Compose could pull the new image, report the container as "Running" and leave it on the old one. The dashboard's own self-update had the same problem.
- A container left on an older image after a pull shows "Pulled – not recreated" instead of "Can't check – Image built locally".

## [0.5.6] - 2026-10-25

### Added

- Choose Auto, Light or Dark on the Account page. The choice is saved to your account, so it follows you to every device.
- Set the weekly digest schedule by picking days and a time, with a plain-English summary of when it sends. A custom cron schedule is still available for anything more complex.

### Changed

- The dashboard follows your device's light or dark setting by default. The header button now cycles Auto, Light and Dark and saves to your account. Anyone who picked a theme with the old button goes back to Auto once after upgrading.
- Pages are wider – up to 1450px instead of 1280px.
- Search sits on the same line as the filter buttons on wider screens and drops below them on phones.
- The Settings page lists its sections down the left and shows one at a time. Settings with two options show as a pair of buttons instead of a drop-down.
- The proxy or tunnel in front of the dashboard shows a greyed-out Stop button that explains why it can't be stopped, in place of the "no Stop" note.

## [0.5.5] - 2026-10-23

### Added

- /healthz now reports the running version, so you can see which release is live without signing in.

### Changed

- The self-update page notices the restart sooner and takes you back to the dashboard as soon as the new version answers.
- The "Updates found" alert waits an hour before sending and leaves out updates that were applied or replaced in the meantime, so it no longer reports updates you've already dealt with.
- Stop is no longer offered for the proxy or tunnel in front of the dashboard, so you can't cut off your own access by accident.

### Fixed

- The check after an update now runs as part of the update job, so the update leaves the list as soon as the job finishes instead of lingering until the next check.

## [0.5.4] - 2026-10-23

### Added

- Restart, stop and start a container from the dashboard. Stop asks you to confirm first.
- Edit a stack's compose file and .env in the dashboard. Changes are checked with `docker compose config` and backed up before saving, and you can choose to save only or to save and apply.
- Restore a stack's config from an earlier backup into the editor, then save it.

## [0.5.3] - 2026-10-19

### Added

- Profile pictures – the Microsoft photo for people who sign in with Microsoft, and Gravatar for everyone else. The dashboard serves the images itself, so browsers never contact Microsoft or Gravatar, and Gravatar can be turned off under Settings.
- When the Mailcow row can't reach the server, admins can preview and apply the fix to the dashboard's compose file (`pid: host` and `privileged: true`). The file is backed up first and existing values are never overwritten.

### Changed

- The Mailcow row explains that `pid: host` or `privileged` is missing instead of showing a raw error.
- The footer stays at the bottom of the window on short pages.
- The add-user forms sit side by side and only show when that sign-in method is turned on.
- Initials ignore a bracketed company name, so "Test User (Org Name)" shows as TU rather than TN.

### Fixed

- The delete-user confirmation shows the email address to type in full rather than cutting it off.

## [0.5.2] - 2026-10-19

### Changed

- Each user now has their own page for role, access, local account and delete actions. The users list keeps a single approve button for people waiting for approval.
- Tidier dashboard layout – badges don't wrap, buttons line up, tables are more compact and the navigation fits phone screens.
- "Send digest now" has moved to Settings, under Weekly digest.
- A footer shows the copyright and the running version.

### Fixed

- The layout check no longer tells you to move the dashboard's own registry login folder, which would have stopped new registry logins reaching the dashboard.

## [0.5.1] - 2026-10-19

### Added

- A download button for two-step recovery codes. The codes are still never stored on the server.

### Changed

- Signing out takes you back to the sign-in page with a note confirming you've signed out.
- The self-update page refreshes by itself once the dashboard is back, so you no longer need to reload it.
- The build pipeline uses Node 24 actions.

### Fixed

- The two-step QR code now scales to fill its box.

## [0.5.0] - 2026-10-18

### Added

- Watch rules per container, set from Docker labels or in the dashboard.
- Skip an update, or snooze it for a while.
- Links to each update's release notes.
- Filters and search on the dashboard.
- A random start delay for scheduled checks, a check on startup and a heartbeat ping for uptime monitoring.
- A layout check that flags anything kept outside /docker.

## [0.4.0] - 2026-10-18

### Added

- Notification channels – Azure Communication Services, SMTP2GO and SMTP.
- Choose which events each channel receives.
- Alerts for failed updates, failing checks, new updates and users waiting for approval.
- A test button for each channel, so you can confirm it works before relying on it.

## [0.3.0] - 2026-10-18

### Added

- Local accounts, with a one-time setup token on first start to create the first admin.
- Two-step sign-in for local accounts.
- Microsoft sign-in, set up from the Settings page.
- Viewer, Operator and Admin roles, which can be mapped from Entra app roles and groups.
- Users and Account pages for managing people and your own sign-in.
- A `reset-admin` command to get back in if you lose access.
- A /healthz endpoint and a Docker health check.

## [0.2.0] - 2026-10-18

### Added

- A Settings page – most settings can now be changed in the dashboard rather than in environment variables.

### Changed

- Sessions are now stored on the server. Everyone needs to sign in again once after upgrading.

## [0.1.2] - 2026-10-18

### Fixed

- Private images are pulled using the registry login, so updates to them no longer fail.
- The self-update helper container is no longer shown on the dashboard or left behind after an update.

## [0.1.1] - 2026-10-18

### Changed

- A refreshed dashboard design.
- Job logs leave out per-layer download progress, so they show only the meaningful steps.

### Fixed

- Images from registries other than Docker Hub are checked properly. Registries that front Docker Hub (such as docker.n8n.io) are checked against Docker Hub directly, avoiding rate-limit failures.

## [0.1.0] - 2026-10-17

### Added

- First release.
