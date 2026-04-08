# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog,
and this project follows Semantic Versioning while in pre-1.0 stabilization.

## [Unreleased]

### Added
- Placeholder for upcoming release notes.

### Changed
- Simplified Docker defaults to a `/config`-first model by removing redundant `APPDATA_PATH`/`LOG_DIR` environment entries from `docker-compose.yml`; `/config` remains the canonical in-container appdata path.
- Updated `docker-compose.yml` volume guidance to map one host appdata root to `/config` and one placeholder/media root for onboarding path selection.
- Removed optional dummy-file bind mount examples from `docker-compose.yml`; onboarding/settings now default to `/config/dummy.mp4` and `/config/coming_soon_dummy.mp4` without extra mounts.
- Updated onboarding and settings path guidance (`services/app_config.py`, `frontend/src/App.tsx`) to emphasize container-visible mounted paths for Library Root and overrides.
- Updated README Docker/setup docs to describe `APPDATA_PATH` and `LOG_DIR` as advanced overrides and document the no-extra-dummy-mount workflow.
- Removed obsolete top-level `version` from `docker-compose.yml` to match modern Compose behavior.

### Removed
- Removed `PLACEHOLDARR_WEBHOOK_URL` from `.env.example` because current webhook URL generation uses UI origin and does not consume this env override.

## [0.8.0] - 2026-04-08

### Added
- Initial structured changelog and release tracking baseline.
- Dedicated development compose file `docker-compose.dev.yml` for local source builds.
- Playback routing guidance UX improvements with clearer grouped controls in settings and onboarding.
- Playback routing summary visibility in Integrations.

### Changed
- Docker runtime defaults are now image-first in `docker-compose.yml`.
- Default Placeholdarr runtime image switched to GHCR: `ghcr.io/theindiearmy/placeholdarr:main-db-latest`.
- Postgres image reference normalized to `docker.io/library/postgres:15-alpine`.
- `docker-compose.override.yml` now focuses on machine-specific overrides and keeps build behavior opt-in.
- Playback fallback timeout configuration was consolidated into the Search Scope flow to reduce redundant controls.

### Fixed
- Prevented internal playback ranking/fallback implementation fields from leaking as raw JSON or low-level fields in UI flows.
- Ensured production compose path stays image-based unless an explicit dev compose merge is used.
