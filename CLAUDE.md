# Docker Manager – notes for Claude

## Git

- Never add Claude (or any AI) as a co-author. No `Co-Authored-By: Claude …` trailer in commit messages, and no "Generated with Claude Code" line in pull request descriptions. This applies to commits made by subagents too – tell them so when dispatching.

## Releases

- The version lives only in `app/__init__.py` (`__version__`). Every release bumps it and adds a matching `## [X.Y.Z]` entry to CHANGELOG.md (a test checks they agree).
- The build (`.github/workflows/build.yml`) publishes each new version as `X.Y.Z` and `X.Y` as well as `latest`, pushes the `vX.Y.Z` git tag and publishes a GitHub release whose notes are the version's CHANGELOG section – so the CHANGELOG entry must be written before the version bump reaches `main`. Don't create version tags by hand, and don't reuse a version: once `vX.Y.Z` exists, later pushes only move `latest`.

