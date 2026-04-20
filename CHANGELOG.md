# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog,
and this project follows Semantic Versioning while in pre-1.0 stabilization.

## [Unreleased]

## [0.9.4] - 2026-04-20

### Added

- **Setup boot shell (first paint)**: Added a branded setup loading shell with shimmer bars and animated status text so `/setup` has a polished boot experience while settings/status are being resolved.
- **Onboarding hero art system**: Added a TMDB poster collage hero with slot-aware center-art replacement for the `yellowBlue` variant, plus unique poster assignments across all 16 grid cells.
- **Hero logo assets**: Added dedicated Placeholdarr SVG variants (`blue`, `yellow`, and opposite-border versions) so hero lockups can use authored assets instead of only CSS recolor/filter approaches.
- **Primary Placeholdarr logo asset swap**: Replaced runtime usage of `Placeholdarr1.svg` with `Placeholdarr_blue.svg` and removed the legacy `Placeholdarr1.svg` file from the frontend asset set.
- **Simularr spectral cyan constant**: Added `SIMULARR_SPECTRAL_CYAN_HEX` in semantic theme tokens as the canonical style-guide cyan reference.
- **Brand tertiary UI utilities**: Added `--brand-accent-tertiary` CSS variable mapping and a reusable `.btn-brand-tertiary` style for actions that should use accent-3 (e.g. Simularr cyan).

### Changed

- **Wizard IA / step flow**: Setup now uses a streamlined step sequence with webhook guidance integrated into ARR and playback modals rather than a dedicated standalone Webhook step.
- **Setup routing/loading behavior**: `/setup` now prefers a setup-first landing/default path until completion is known, avoids duplicate settings fetches on first setup load, and uses lighter status polling after the initial payload to reduce latency and avoid clobbering in-progress edits.
- **Onboarding hero branding and contrast**: Reworked hero treatment to support blue/yellow duotone variants, centered lockup scaling, stronger logo/wordmark outlines, and bottom blending back into the wizard shell background.
- **Placeholdarr mark rendering**: Replaced path-built logo mark usage with SVG asset-based rendering for Placeholdarr/Simularr lockups.
- **Simularr dark token tuning**: Refined accent-3/label/border/glass/nav-hover color relationships to align with the Spectral Data palette and improve cross-surface consistency.
- **Settings copy clarity**: Updated several settings labels/descriptions for startup sync and calendar/status controls to be more direct and easier to scan.
- **ARR instance identity generation**: New ARR rows now default to deterministic instance ids (e.g. `radarr_primary`, `sonarr_secondary`) while preserving existing UUID ids and alias continuity.
- **Media-server connection validation**: Jellyfin/Emby tests now parse and validate JSON signatures so cross-wiring one server type into the other form returns explicit corrective guidance.
- **Font loading behavior**: Material Symbols now loads with `display=block` to reduce icon FOIT/FOUT flashes.

### Fixed

- **Startup lite Radarr duplicate warning**: Fixed targeted movie upsert identity to match `(tmdbid, instance_key)` uniqueness (with legacy `instance_id` fallback), preventing duplicate-key warnings during startup lite history sync.
- **Series upsert identity parity**: Updated series targeted/full upsert matching to follow `instance_key` identity consistently, reducing cross-instance collision risk under multi-instance ARR configs.

### Removed

- **Legacy logo path helper**: Removed `frontend/src/assets/logoMarkPaths.ts` after migrating to asset-based logo rendering.

## [0.9.3] - 2026-04-19

### Added

- **Setup URLs on the server**: `GET /setup` and `GET /setup/{path}` return the same SPA shell as the dashboard so `/setup` bookmarks, refresh, and deep links load the app instead of a blank or wrong page.
- **First-run routing**: Incomplete setup uses `/setup` as the default landing path; other routes redirect there until you finish; visiting `/setup` after completion sends you to **Activity**.
- **Onboarding wizard overhaul**: A dedicated, full-screen **Setup** experience with a five-step journey—**Paths**, **Media Servers**, **ARR Services**, **Webhook Setup**, and **Behavior**—including a stepper, scroll-to-top when changing steps, a **connection warning** when any test in the step failed, and **Continue** gates (library root required; at least one enabled media server with a successful **Test** or saved credentials; Radarr/Sonarr **primary** test success or already-saved primary; webhook step requires at least one configured ARR instance before showing instructions).
- **Paths step (wizard)**: Library root, folder profiles, and optional per-library overrides use a wizard-specific layout—grouped panels and overrides folded behind an expandable **Custom folder per library** section so the first screen stays approachable.
- **Media servers step (wizard)**: Card-style integration chooser with **slide-over** detail panels, per-field **Test**, and **Cancel** behavior that reverts or clears fields when you opened **Add** and leave without committing.
- **ARR step (wizard)**: **Slot / slide-over** editor (Overseerr-style) with primary vs secondary toggles (secondary locked until primary **Test** passes), **Disconnect** confirmation, **webhook helper** modal, duplicate-instance-key prevention, stable `**instance_id`** on new slots, and messaging when labels or keys change.
- **Webhook setup step**: Per-instance Radarr/Sonarr instructions with stable `**instance_id`** webhook URLs, required events listed, and **copy** actions (including a path that works on **plain HTTP** where the Clipboard API is limited). When Plex, Jellyfin, or Emby is enabled, adds **playback webhook** walkthroughs (e.g. Tautulli, Jellyfin Webhook plugin, Emby) with **JSON payload templates** and copy buttons.
- **Behavior step (wizard)**: Walks **Library sync**, **Calendar**, **Lookahead**, **Status updates**, and related options across subsections; includes **long-form onboarding copy** for lookahead and status projection (separate from the shorter blurbs in **Settings**) and wizard-specific explanations for **startup sync** (lite vs full vs auto, restart vs wizard, media-server scan caveats).
- **Branding and theme across setup and dashboard**: New and updated fonts, semantic theme tokens, logos and service marks, and scoped **brand** styling so first-run setup and the main app read as one product.
- **Local development quality-of-life**: The Vite dev server can be opened from another device on your LAN, which makes phone or second-machine testing less painful.
- **Reconcile after ARR edits**: Removing or reshuffling Radarr/Sonarr instances triggers cleanup of orphaned database rows tied to old instance keys, then a focused reconcile so the library view matches what you actually configured.
- **Safer cleanup with multiple Radarr/Sonarr instances**: On-disk placeholder removal checks whether another configured instance still “owns” the same movie or series before deleting files, so a second copy on another server does not get wiped by accident.
- **Orphan placeholder maintenance**: Startup and scheduled passes can remove stray placeholder files and rows that lost their links (scoped to your library roots and basic safety rules) and nudge a library refresh when something real was deleted.

### Changed

- **Wizard vs settings use the same components**: Library paths and ARR **slot** editing reuse shared building blocks with `**wizard`** vs `**settings`** layouts (and onboarding vs settings intros where it matters) so what you learned during setup matches what you edit later.
- **Radarr vs Sonarr connection tests**: The “test connection” action reads the remote app’s JSON status so mismatched app types fail with a clear message instead of looking like a mysterious network error.
- **Saving ARR instances**: Each slot keeps a stable identity for webhooks; renames can carry old keys as aliases so existing URLs and history keep working; saves can return a short summary when background reconcile ran.
- **Webhook routing**: Instance targeting accepts stable ids, legacy keys, and aliases; invalid combinations fail early with a clear HTTP error instead of misrouting silently.
- **Advanced settings noise**: Several expert-only knobs (poll intervals, always-on NFO creation, deprecated calendar dummy toggle, and similar) are no longer surfaced in the dashboard settings schema—use environment variables when you truly need them.
- **Help text in settings**: Clearer explanations for startup sync modes (lite vs full vs auto), preferred Radarr release-date behavior, TV lookahead, and how status text is projected into placeholders.
- **NFO generation**: Movie and episode NFO sidecars are always written during materialization, matching the enforced “NFOs on” product default.
- **Coming Soon dummy artwork**: Selection no longer depends on a deprecated primary-vs-coming-soon switch; Coming Soon uses the dedicated dummy when you configured one.
- **Playback at the end of what we know about a show**: When you finish the last episode of the highest season Placeholdarr has modeled, playback metadata can signal end-of-stored-range so Sonarr whole-series behavior (including future seasons) lines up with Episode-mode expectations (includes `bfd3875`).
- **Placeholder status history**: Fewer chatty history rows from calendar countdown churn; the flip from Coming Soon to Request on release day stays visible with an explicit reason.
- **Status projection mode**: Legacy `off` is treated as `summary`; the UI only offers summary, title, or both.
- **Startup robustness**: Tighter import ordering for clean shutdown in edge cases; if the HTTP port is already taken, the process exits with guidance to set `PLACEHOLDARR_PORT` instead of trying to kill the other process automatically.

### Removed

- **Dashboard-only exposure of expert env knobs**: Removed several advanced keys from the dashboard settings schema (deprecated calendar lookahead dummy mode, placeholder NFO toggle, generic check interval, queue monitor poll/refresh tuning); configure via environment variables when required.

### Fixed

- **Webhook test events**: Sonarr/Radarr connectivity tests are recognized end-to-end instead of being treated like unknown webhook traffic.
- **Materialization hygiene**: Removed a duplicate unreachable return in the materialization path so stats and flow stay honest.

## [0.9.2] - 2026-04-16

### Changed

- **Placeholder path drift reconciliation (movies + TV)**:
  - Lite sync includes ARR path-drift ID discovery (Radarr/Sonarr API path vs stored DB path), then runs targeted ID refresh for drifted content even when history does not carry a usable rename signal.
  - Determination classifies path-drifted placeholders as `obsolete_placeholder`, and materialization applies obsolete-first then needs so relocation completes within one lifecycle pass.
  - Drift evaluation includes rows with stored placeholder paths even when `has_placeholder` is transiently false.
  - TV relocation cleanup prunes empty old season/series directories from deleted candidate paths so obsolete trees are removed after migration.

## [0.9.1] - 2026-04-15

### Added

- **Internal activity markers**: Added `services/activity_markers.py` to persist authoritative startup and calendar sync timestamps into `EventLog` (`internal_dashboard_startup_source_of_truth`, `internal_dashboard_calendar_date_refresh`) so activity timing survives restarts without fragile log parsing anchors.
- **Placeholder status history rows**: Added `placeholder_status_changed` event logging from status orchestration and surfaced those transitions in `/api/activity/placeholders` with user-facing status text.
- **Grouped activity expansion**: Grouped webhook activity rows now carry expandable `grouped_events` details in API payloads and UI rendering.
- **Queue monitor timeout setting in UI**: Exposed `QUEUE_MONITOR_SEARCH_TIMEOUT_SECONDS` in Advanced settings metadata.

### Changed

- **Jellyfin ID resolution (movies + TV)**: Replaced legacy provider-id query assumptions with documented Jellyfin `GetItems` filters (`SearchTerm`, `IncludeItemTypes`, `Years`, `Fields=ProviderIds,...`) plus exact client-side `ProviderIds` matching; hardened series/episode resolution to fail-safe matching; and removed permissive/fuzzy fallback paths that could target unrelated items.
- **Jellyfin TV episode lookup finalization**: Normalized Jellyfin `/Items` query parameter casing to documented form and aligned episode resolution to a deterministic `parentId + recursive` query with strict in-code series/season/episode filtering, preferring exact TVDB provider matches and failing safe on ambiguity.
- **TV projection scope tightening**: Status projection for TV now updates **episode** metadata only (not series-level projection text writes) across direct player projection paths.
- **Series NFO status projection removal**: `tvshow.nfo` generation no longer writes status-projected title/plot or status tag text; episode NFO status projection remains intact and still follows projection config mode/scope.
- **Jellyfin cache validation**: Movie/series cached Jellyfin IDs are validated before reuse; mismatches trigger re-resolution instead of persistent wrong-item updates.
- **Direct player projection logging**: Added per-player outcome lines plus a batch summary (`ProjectionBatchSummary`) so projection behavior is diagnosable per run without requiring deep trace logs.
- **Queue monitor semantics**: Empty Arr queue no longer clears activity snapshot; UI row now reflects ongoing search monitoring and timeout-driven terminal handling.
- **Queue monitor terminal behavior**: Searches that never enter queue now timeout to `NOT_FOUND` with user-facing reason `NO QUALIFYING RELEASE FOUND`, and stop active queue-refresh nudging once terminal.
- **Status naming alignment**: Canonicalized `NOT_FOUND` handling across orchestration, projection, and dashboard humanization to display `NO QUALIFYING RELEASE FOUND`.
- **Activity history persistence**: Dashboard activity now includes historical internal sync/calendar marker rows (one row per `EventLog` marker) while preserving rich details for the latest startup sync row.
- **Calendar card density/UI**: Reduced card sizing/padding and removed redundant date/year/countdown metadata; movie cards now emphasize release type and TV cards emphasize episode identity.

### Fixed

- **Post-restart activity timestamps**: Fixed stale "hours ago" startup/calendar times by preferring DB-backed marker timestamps and hardening log-file fallback selection.
- **Lite sync details regression**: Restored rich lite/full sync stats on the latest run while still showing historical rows.
- **Jellyfin/Emby direct update method handling**: Hardened direct item text update attempts with method/payload fallback order and clearer failure diagnostics.

## [0.9.0] - 2026-04-14

### Added

- **Direct player metadata projection**: After placeholder NFO sidecars are rewritten, optionally push **projected title and summary** (bracketed status per `PLACEHOLDER_STATUS_PROJECTION_MODE`) straight to **Plex, Jellyfin, and Emby** via their item-update APIs. Uses cached server item ids when present, otherwise resolves items by TMDB (movies) or TVDB + season/episode (TV). This is **targeted text updates**, not “wait for a full library refresh to re-read NFOs” as the only fast path for UI text.
- `**services/media_servers/player_metadata_refresh.py`**: Implements the above batch push and identity persistence helpers used from the NFO refresh job path.
- `**PLACEHOLDER_STATUS_UPDATES`**: New scope for **which** statuses receive bracket projection in title/summary (`OFF`, `REQUEST`, or `ALL`), evaluated in `services/status_projection.py` alongside the existing projection mode; exposed in Advanced settings.
- **Queue monitor**: Optional `**QUEUE_MONITOR_POLL_INTERVAL_SECONDS`** override (separate from `CHECK_INTERVAL`); periodic **Radarr/Sonarr `RefreshMonitoredDownloads`** while queue-like placeholders exist (`QUEUE_MONITOR_REFRESH_MONITORED_DOWNLOADS_INTERVAL_SECONDS`, staggered first run via `QUEUE_MONITOR_REFRESH_STAGGER_SECONDS`); shared poll context; ARR API trigger helpers.
- **Activity snapshot** (`services/activity_snapshot.py`): In-memory queue download snapshot for richer **Activity** dashboard rows.
- **Calendar API**: Episodes that share the same air date and series merge into one item with `SxxEyy (+N)` subtitles; payloads include `season_number`, `episode_number`, and optional `group_episode_ids` / `group_episode_count`.
- **Calendar UI**: Hero **calendar-style** date (month + day); TV spotlight uses **series** synopsis, then **per-episode** rows (code + title) expandable to still + episode overview; **single-episode** days start expanded.
- **Dashboard**: Lite sync startup card shows **“Checking…”** for library refresh until materialization completes; refresh lines also honor `movie_refresh_triggered` / `tv_refresh_triggered` from materialization stats; expanded activity parsing (e.g. calendar sync row with lookahead stats).
- **Episode Runtime**: Added `sonarr_runtime` field to episodes, populated from Sonarr API for richer metadata.
- **Detailed Episode Info**: Now capture detailed episode information from Sonarr, including extended metadata and screenshots.
- **NFO Screenshot Art**: Added support for embedding episode screenshot art in generated NFO files for richer Plex display.
- **Refresh Throttle**: Introduced `library_refresh_throttle` table and new refresh throttling logic to prevent excessive section refreshes.
- **Jellyfin helper**: Add `get_jellyfin_file_path()` in `services/media_servers/jellyfin.py` and use it to resolve file paths during playback fallback when payloads lack an explicit path.

### Changed

- **Status intent → player push**: `StatusIntent.wants_player_metadata_refresh_after_nfo()` skips direct projection for **initial** materialization that ends on plain **REQUEST** (NFO already reflects REQUEST; broad discovery stays on library refresh). Other NFO-driving intents request the direct player-metadata pass.
- **NFO refresh jobs**: Payload supports `**player_metadata_refresh`** (per job / per placeholder merge) so batches can run **NFO-only** or **NFO + direct projection** as needed (`status_reconciler`).
- **Queue monitor producer**: Refactored polling context, integration with activity snapshot and ARR queue nudges, and related orchestration tweaks.
- **Media server clients**: Plex / Jellyfin / Emby additions for **search-by-provider-id** and **item text updates** used by direct projection; Plex lookup/identity helpers extended for resolution paths.
- **Materialization stats**: `created_or_exists` with **no write** (`already_present`) increments `**noop`** instead of `**created`**, so logs and dashboards match real file creates.

### Removed

- **Observation System**: All observation, hybrid, and trail job code, database tables, and config settings.
- **Primer Observation/Projection**: Primer no longer runs observation or status projection for seeded placeholders.
- **Status Projection Jobs**: Status projection job and reconciliation logic removed; status projection is now handled inline or skipped.

### Fixed

- **Materialization metrics**: Correct `created` vs `noop` accounting when placeholder files already exist on disk.
- **Refresh Lease Handling**: Improved error handling and atomicity in refresh throttle logic.
- **Startup/Shutdown**: Minor fixes to startup/shutdown signal handling and Compose dependency ordering.
- **NFO/Metadata Consistency**: Improved NFO generation and metadata consistency for new episode fields and art.

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

- Updated GHCR workflow to publish semver tags on git tag pushes (`*.*.`*) and continue publishing branch `-latest` tags.
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