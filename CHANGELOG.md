# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog,
and this project follows Semantic Versioning while in pre-1.0 stabilization.

## [Unreleased]

### Added
- Placeholder for upcoming release notes.

## [0.8.5] - 2026-04-09

### Added
- Default dummy media files provisioning at startup to ensure onboarding has files to work with.
- Schema resilience checks via `_ensure_core_tables()` with Alembic fallback to prevent startup crashes from migration gaps.
- Centralized, idempotent background service initialization (`start_runtime_background_services()`) for consistent worker/scheduler startup timing.
- Webhook accept-but-ignore gate during onboarding to prevent job queue noise from pre-setup events; senders receive `{"status":"accepted","ignored":true,"reason":"onboarding_incomplete"}`.
- New ARR instance helper functions (`_arr_instance_maps()`, `_arr_instance_meta()`) for cleaner instance metadata lookups in dashboard routes.

### Changed
- Post-onboarding settings completion now triggers worker/scheduler startup immediately (no app restart required).
- Refactored `_arr_base_url()` to delegate instance resolution to `settings.resolve_arr_endpoint()` for centralized routing logic.
- Startup gate (`startup_sync_complete`) is now guaranteed to be set during onboarding scenario (via post-onboarding sync) allowing workers to process queued jobs.
- Updated onboarding status logging to report "onboarding-incomplete" by default on read failures (defensive behavior).

### Fixed
- Fixed worker and scheduler startup deferral bug: services were never started after onboarding completion without app restart.
- Fixed queued->pending status migration to be fault-tolerant; if movie/series/episode tables unavailable, migration is skipped with warning instead of crashing.

## [0.8.4] - 2026-04-09

### Changed
- Simplified onboarding paths to a single `LIBRARY_ROOT` flow with derived `movies` and `tv` folders instead of separate placeholder path/profile branches.
- Reworked onboarding progression so `Save & Continue` stays blocked until each key step has meaningful configured input, including confirmed media-server and ARR connection tests where applicable.
- Updated ARR setup to support optional primary Radarr/Sonarr usage, with secondary instance toggles unlocking only after a successful primary connection test.
- Refined ARR playback/placeholder routing controls so fallback messaging, timeout visibility, and disabled states better reflect real routing behavior.
- Moved Plex library creation guidance out of the onboarding flow and into the Plex library ID fields where it is actually needed.
- Updated ARR webhook guidance in UI and docs to use per-instance setup instructions and current required Radarr/Sonarr webhook event names.

### Added
- Added runtime creation of derived `movies` and `tv` folders when `LIBRARY_ROOT` is saved, including open-permission handling based on configured directory mode.
- Added default dummy media provisioning to `/config` in the container image, with placeholder dummy-path resolution falling back through `/config` and in-image defaults.
- Added contextual logging for settings saves and onboarding partial-save requests to improve onboarding diagnostics.

### Fixed
- Fixed webhook setup rendering so all enabled ARR instances, including primary instances, appear in setup instructions instead of only secondary entries.
- Fixed ARR primary instance labeling so the UI consistently uses `Primary` instead of mixing `Primary` and `Standard`.
- Fixed fallback controls to disable automatically when they are not applicable, when fallback is turned off, or when all unlocked search behaviors already target both instances.
- Fixed onboarding/settings guidance mismatches around ARR webhook triggers, fallback behavior, and Plex library ID setup.

## [0.8.3] - 2026-04-09

### Changed
- Split settings into `Media Integrations` and `ARR Integrations` for clearer setup flow and onboarding parity.
- Reworked ARR instance management to a slot-based model (primary/secondary) with explicit limits of up to 2 Radarr and 2 Sonarr instances per deployment.
- Replaced playback ranking/search-all controls with explicit dropdown routing modes for placeholder playback and real-file playback.
- Updated fallback behavior messaging and handling so missing/deleted preferred-instance rows fall back immediately, while delayed fallback remains timeout-based after attempted searches.
- Enforced standard placeholder profile as always enabled in the simplified path model.
- Updated frontend toolchain to Vite 6 and `@vitejs/plugin-react` 5.
- Updated default Postgres image in compose from `postgres:15-alpine` to `postgres:18`.

### Removed
- Removed legacy playback/ranking configuration fields (`MOVIE_INSTANCE_RANKING`, `TV_INSTANCE_RANKING`, `MOVIE_PLAYBACK_SEARCH_ALL_INSTANCES`, `TV_PLAYBACK_SEARCH_ALL_INSTANCES`, `PLAYBACK_SEARCH_PREFERENCE`) from settings flows and backend parsing.
- Removed the user-facing `Enable playback event handlers` setting; playback event handling is now treated as core always-on behavior.
- Removed legacy anime profile/path fields from the simplified paths experience.

### Fixed
- Fixed ARR instance editor input remount/focus loss by using deterministic row IDs.
- Fixed secondary ARR slot behavior to prevent incorrect value promotion into primary slots.
- Prevented stale/legacy internal playback keys from leaking into settings wizard and ARR sections.

## [0.8.2] - 2026-04-08

### Changed
- Updated GHCR workflow to publish semver tags on git tag pushes (`*.*.*`) and continue publishing branch `-latest` tags.
- Updated Docker compose defaults to make `.env` optional by removing the required `env_file` dependency.
- Simplified `.env.example` to focused infrastructure/advanced overrides instead of onboarding-managed behavior settings.
- Updated README configuration guidance to reflect optional `.env` usage and current override variables.
- Expanded Logs UI filter levels from `all/warn/error` to `all/debug/info/warn/error/critical`.
- Expanded `/api/logs` filtering to support threshold-based level filtering for `debug`, `info`, `warn`, `error`, and `critical`.

## [0.8.1] - 2026-04-08

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
