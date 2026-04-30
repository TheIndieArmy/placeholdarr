# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog,
and this project follows Semantic Versioning while in pre-1.0 stabilization.

## [Unreleased]

### Changed

- **REQUEST NFO backfill now runs in bulk NFO-only mode**
  - Startup REQUEST backfill enqueues `nfo_refresh` jobs with direct player projection disabled so large libraries are not bottlenecked by per-item Plex/Jellyfin/Emby metadata writes during catch-up.
- **Backfill completion now triggers one library refresh**
  - Backfill jobs are tagged with a run id; when the last job in that run completes, the app triggers a single section refresh (`movies + episodes`) so players pick up updated NFO text in one pass.
- **Backfill queue isolation**
  - REQUEST backfill enqueues with pending-job merge disabled to avoid inheriting older mixed payloads and to keep backfill behavior deterministic.
- **Persisted projected display status in DB**
  - Added `placeholder.display_status_projected` so the user-facing status text is stored persistently (including REQUEST runtime bracket text like `[1h 43m · REQUEST]`) whenever status is written by orchestrator/materializer/import-grace paths.

## [0.9.9] - 2026-04-30

### Summary

- REQUEST placeholders now have a one-time startup NFO refresh backfill so summary/runtime projection catches up without waiting for status transitions.
- REQUEST summary projection now includes duration in the same status bracket for clearer at-a-glance metadata. Done through a one-time startup NFO refresh backfill to add runtime to all "[REQUEST]" status summaries.
- Placeholder presence drift between `Episode`/`Movie` and linked `Placeholder` rows is now corrected in runtime refresh paths, preventing large libraries from being skipped by NFO refresh jobs.
- Webhook setup instructions can now be forced to a specific externally reachable base URL.

### Added

- **Webhook base URL override for setup instructions**
  - Added `WEBHOOK_BASE_URL` support so generated webhook guidance can use a fixed external URL instead of relying only on request-origin inference.
- **REQUEST status NFO backfill on startup**
  - After materialization, the app can run a one-time backfill (tracked in `app_config`) that enqueues `nfo_refresh` jobs for active placeholders with `display_status` = `REQUEST`. Once successfully queued, it marks complete and does not rerun on later startups. This refreshes sidecar NFOs to pick up new summary/runtime wording without waiting for a status transition.

### Changed

- **REQUEST placeholder summaries show duration in the status bracket**
  - For `REQUEST` only, the summary prefix includes rounded runtime from Radarr/Sonarr minutes inside the same brackets, e.g. `[1h 5m · REQUEST] …`. Legacy `~45m · [REQUEST]` and `[1h 5m - REQUEST]` text is still stripped when re-projecting. Plex/Jellyfin/Emby direct projection uses the same rule when the projected status is `REQUEST`. Episodes fall back to series runtime when episode runtime is missing.

### Fixed

- **Placeholder presence drift that could skip TV NFO refreshes**
  - Presence-refresh paths now mirror canonical `Episode`/`Movie` placeholder truth back into linked `Placeholder` rows (including path realignment), reducing long-lived drift where episodes existed on disk but `Placeholder.has_placeholder` stayed false.
  - NFO refresh processing now self-heals presence drift by honoring linked entity placeholder truth when deciding whether a queued placeholder should be refreshed.

## [0.9.8] - 2026-04-29

### Summary

- Lite startup activity is now easier to read at a glance, with user-focused sections (`Scope Checked`, `What Changed`, `Why Items Were Skipped`) and less technical noise.
- Skipped placeholder reasons are clearer, with separate buckets for `Not yet aired` and `Air date unknown`, plus tooltip context explaining the Sonarr-based future assumption behavior.
- Missing-air-date episode handling is more practical: null-date “middle” episodes can now become eligible when later episodes in the same series have known dates inside the lookahead window.
- Targeted Sonarr sync now reconciles episodes removed upstream by soft-deleting missing DB episode rows, preventing repeated “changed series” churn from episode-count mismatch drift.
- Dashboard and shared UI text are slightly larger for readability; primary movie/series hero titles and the calendar spotlight headline keep their previous display scale.

### Changed

- **Startup lite activity card UX (user-focused)**
  - Reworked startup activity sections from technical pipeline labels to user-facing summaries:
    - `Scope Checked`
    - `What Changed`
    - `Why Items Were Skipped`
    - conditional `Issues & Alerts`
  - Updated summary copy to emphasize outcomes (created/removed/up-to-date) instead of internals.
  - Removed file-level counters from the lite sync activity card details (kept for placeholder history workflows).
  - “Scope Checked” counts now use the larger of catalog-seen totals and determination totals so scoped lite runs do not show zero scope when skip-reason metrics are non-zero.
- **Skipped-reason clarity for lite sync**
  - Split skipped not-needed counts into separate buckets:
    - `Not yet aired`
    - `Air date unknown`
  - Added tooltip support for activity metrics (custom “?” control; avoids duplicate native browser tooltips) and included non-technical context for unknown air dates:
    - “If Sonarr has no date for an episode, Placeholdarr checks later episodes. If all later dated episodes are still in the future, this episode is treated as not yet aired.”
- **Lite sync activity layout**
  - Progress detail grid uses responsive columns with a sensible max width so cards use wide screens better without overstretching.
- **Missing-air-date determination refinement**
  - Added a “middle episode” heuristic for episodes with no air date: if a later episode in the same series has a known date within the configured lookahead horizon, the null-date episode is treated as in-window for placeholder lifecycle decisions.
  - Applied this behavior in both full-scan and scoped determination paths.
- **Startup/worker logging clarity**
  - Materialization now emits user-facing INFO lines for actual placeholder creation events.
  - NFO follow-up processing logs were downgraded from prominent INFO phrasing to technical DEBUG phrasing to reduce operator confusion during create-heavy runs.
  - End-of-batch materialization INFO summary text now prioritizes user outcomes (`placeholders_created`, `already_present`, `deleted`, `errors`).
- **Dashboard typography**
  - Increased typical UI text sizes by 2px across the dashboard (Tailwind utilities and shared CSS helpers such as `ui-field-description`), excluding primary detail-page hero titles (`h1` on movie/series) and the calendar spotlight title so large display type stays unchanged.

### Fixed

- **Targeted Sonarr sync now reconciles removed episodes**
  - During `sync_sonarr_series_by_ids`, episodes that remain active in DB but are missing from Sonarr’s current episode list for that series are now soft-deleted (`is_deleted=true`).
  - This resolves repeated lite startup recataloging of unchanged series caused by persistent `total_episodes_vs_DB` mismatches after upstream Sonarr/TVDB episode-list churn.
- **npm audit (frontend)**
  - Addressed the moderate-severity `postcss` advisory by updating the dashboard dependency pin (`frontend/package.json` / lockfile).

## [0.9.7] - 2026-04-28

### Summary for users

- **Startup lite logs are clearer at high row counts.** Reconciliation output now calls out capped query behavior and gives better visibility into what was actually selected.
- **Specials backfill is more efficient and less noisy.** Sonarr episode fetch/filtering for season 0 backfill was tightened, and progress logging was tuned for long runs.
- **Operational logging controls were refined.** Logging configuration and capture behavior were updated to improve diagnostics.

### Changed

- **Specials backfill Sonarr fetch path**
  - Season-scoped Sonarr episode fetching/filtering for specials backfill was improved to reduce unnecessary episode processing.
  - Backfill sync stats now include clearer season 0 progress accounting.
- **Specials backfill logging cadence**
  - Progress logging for long specials backfill runs was reworked (including interval-based/conditional progress output) to reduce log spam while keeping useful checkpoints.
- **Lite reconciliation observability**
  - Startup lite reconciliation logs now include better capped-row messaging and summary detail when per-query limits are reached.
- **Logging/config capture updates**
  - Logging-related config and capture surfaces were refined (`core/logger.py`, `core/config.py`, dashboard log API/types, and related startup/runtime logging touchpoints).

### Fixed

- **Specials backfill progress signal quality**
  - Reduced misleading or overly chatty progress lines during long season 0 backfill operations, making run state easier to interpret.

## [0.9.6.1] - 2026-04-27 [HOTFIX]

### Summary for users

- **Settings works again after refresh.** Opening or reloading **Settings** no longer leaves you on a blank/black main panel while the page loads your configuration.

### Fixed

- **Dashboard Settings blank screen on refresh**: `SettingsPanel` called `useMemo` for `allSettingsFieldsByKey` only after the settings payload arrived, but returned early when `payload` was still `null`. That violated the Rules of Hooks when the first fetch completed, crashed the React tree, and showed an empty/black content area on `/settings`. The field map memo now runs on every render (empty until sections exist). (`frontend/src/App.tsx`)

## [0.9.6] - 2026-04-27

### Summary

- **Restarts feel lighter.** After an update or restart, Placeholdarr spends less time “touching everything” and focuses on what actually changed in your Radarr/Sonarr libraries.
- **Title pages are more helpful.** On a movie or show, you get a clearer picture of what’s on disk vs placeholder, and quick links to open the right Radarr or Sonarr when you have more than one.
- **Removals match reality.** If something vanishes from Sonarr or files get deleted, placeholders and activity history line up better so you’re not left with stale clutter.
- **Turning on specials can catch up once.** If you enable specials, the next lite run can bring older special episodes into line without you micromanaging it.

### Added

- **Library list summary mode**: `GET /api/library` accepts `summary=true` to omit large `overview` and `backdrop_url` fields for smaller JSON on periodic refresh.
- **Dashboard library IA**: Sidebar **Library** opens **Movies** at `/library`; **TV** lives at `/library/tv` with nested nav (same pattern as Settings). Per-shelf filters persist in `sessionStorage` (`placeholdarr:library-shelf-filter:movies` / `:tv`); legacy `placeholdarr:library-filter` is migrated once on load.
- **Detail ARR deep links**: Movie and series detail APIs include `**arr_instance_links`** (label + URL per Radarr/Sonarr instance that holds the same TMDB/TVDB title). `**arr_instance_links`** now also carries `**has_file` / `has_placeholder**` per movie row and `**episode_files` / `episode_placeholders**` per series row (Sonarr episode aggregates). The dashboard shows a bottom **launch row** with the service logo and configured instance name; the calendar spotlight can open multiple instance links when present.
- **Settings → Media Integrations**: **Webhook URL** on each connected Plex/Jellyfin/Emby card opens the same playback webhook setup modal as onboarding (shared `PlaybackWebhookSetupModal`).
- **Lite sync catalog tombstones**: Startup lite compares DB rows to the live Radarr/Sonarr catalogs and targets IDs missing from the API for tombstoning (`movies_catalog_removed` / `series_catalog_removed`), alongside path-drift discovery.
- **Series tombstone bulk cleanup path**: Deleted Sonarr series use a series-level placeholder cleanup routine with progress logs, safe full-tree deletion checks, and aggregate history metadata for grouped placeholder activity UI rows.
- **Webhook placeholder activity outcomes**: Radarr/Sonarr webhook handlers append `PlaceholderActivityHistory` rows for series add, movie/episode import (grace scheduling), movie file delete, movie delete, and episode file delete (with aggregate materialization stats), alongside the existing movie-add outcome row. Series delete webhooks continue to rely on the materializer’s aggregate bulk-delete history row to avoid duplicates. Import grace **finalize** jobs record one outcome row per movie/episode after deferred materialization.
- **Lite pre-discovery reconciliation** (`lite_reconcile.py`): Bounded DB queries seed scoped determination/materialization for placeholder/path mismatches and triple-false rows before ARR catalog work; counts surface on `startup_sync_stats.lite_reconciliation_pre`.
- **Specials retroactive backfill trigger**: When `INCLUDE_SPECIALS` is enabled, settings persist a one-time startup marker so the next startup lite run can refresh specials broadly and seed scoped determination for season 0.

### Changed

- **Startup lite is catalog-diff + targeted sync (less churn, faster restarts).**
  - Per-instance snapshot diff against live Radarr/Sonarr (`/movie` + `/series`) vs DB; targeted `sync_radarr_movies_by_ids` / `sync_sonarr_series_by_ids` for changed IDs only.
  - Lite mode skips full filesystem scan and global placeholder reconcile; runs per-entity placeholder truth refresh before scoped determination.
  - Targeted sync returns `touched_movie_row_ids` / `touched_episode_row_ids` merged with reconciliation seeds; upsert helpers return created/changed signals to avoid no-op churn.
- **Startup and activity progress UX**
  - Early `system_activity_history` snapshots during lite discovery; richer catalog logs (title groupings + refresh summaries); Sonarr targeted sync logs `Series i/N` with episode counters and elapsed time.
  - Determination completion anchors standardized to `Determination · full_scan · complete` / `Determination · scoped · complete`; dashboard determination metrics clarified.
  - Startup sync mode copy describes catalog diff + targeted sync; activity feed collapses duplicate startup progress snapshots per run.
  - Startup snapshots include explicit `fs_scan` and pre-determination phases where applicable; determination logs steady progress; lite/full materialization rows show deleted/files_deleted; calendar sync activity uses persisted marker snapshots only; startup marker payloads slimmed for dashboard backfill when logs truncate.
- **Series tombstone cleanup**: Prefer safe whole-folder removal when checks pass; per-file/prune fallback when not.
- **Specials model**: Sonarr sync/events always ingest season 0 episode rows; `INCLUDE_SPECIALS` gates determination/materialization, not capture.
- **Movie & series detail API**: When **ARR_INSTANCES_JSON** lists Radarr / Sonarr instances, `arr_instance_links` includes **every configured slot** for that type in priority order; absent slots use `present: false` and a **base ARR UI** URL so the dashboard can show **"-"**. **Movies** pad Radarr; **series** pad Sonarr and add `episode_total` per row. Rows match slots by **instance_key**, **instance_id**, and **instance_key_aliases**; extra local rows for the same TMDB/TVDB are **appended** when not already emitted.
- **Dashboard polling**: Slower polling in background tabs, refetch on focus, `/api/stats` only on Activity tab, **summary** library payloads by default, debounced full library fetch when header search may match overview text.
- **Detail ARR instance buttons**: Dedicated surface so light mode keeps a **deep navy** fill on instance chips.
- **Library API & grid**: `/api/library` merges rows sharing the same **TMDB** (movies) or **TVDB** (series) across instances; `**instance_label`** cleared on list rows; grid styling updates (year accent, no type pill, no instance badges in grid).
- **Movie & series detail (dashboard)**: Hero refresh (year accent, stronger fade), navy instance tiles with per-slot Radarr/Sonarr counts, redundant stat tiles removed, `arr_instance_links` always returned as an array (no silent fallback to a single `arr_link`).
- **API-offline UX**: Full-body reconnect panel while API is unreachable.

### Fixed

- **Lite Sonarr catalog diff**: Compare DB episode totals to Sonarr `statistics.totalEpisodeCount`; exclude `Episode.is_deleted` from aggregate counts; fix ArrState variable shadowing so `last_history_checked_at` persists after catalog diagnostics; reduce season 0 triple-flag reconciliation noise when `INCLUDE_SPECIALS` is disabled.
- **Settings first paint**: Fetch `/api/settings/current` immediately; show explicit empty-state when `sections` is empty.
- **ARR file-delete webhooks**: On `movie_file_deleted` / `episode_file_deleted`, clear denormalized `has_placeholder` / `placeholder_filepath` and mark linked `Placeholder` rows inactive before determination so materialization can recreate dummies when needed.
- **Lite Sonarr delete convergence**: Targeted `sync_sonarr_series_by_ids` cascades not-found tombstones to seasons/episodes (parity with full sync).
- **Placeholder reconcile path drift**: `canonical_path_realigned` realignment to reduce obsolete/needs churn after folder moves.
- **Materializer path-row ownership**: Avoid stealing path rows across instances (same-path contention).
- **FS startup scan**: Incremental scan with canonical remap safeguards so path-only rows are not mass-marked missing during startup lite.

### Removed

- Unused ARR history pagination helpers in `arr_api.py` (startup lite no longer reads `/history` for discovery).
- Obsolete dashboard log scrapers for deprecated `Startup lite targeted …` JSON progress lines.

## [0.9.5] - 2026-04-21

### Added

- **Theme preference persistence**: The dashboard remembers **light** vs **dark** via `localStorage` (`placeholdarr:studio-theme-mode`) across reloads and new tabs.
- **Settings subsection deep links (server)**: `GET /settings/{path:path}` serves the React shell so bookmarks, refresh, and direct URLs under `/settings/...` load the app (same pattern as `/setup/*`).

### Changed

- **Placeholdarr studio shell (dashboard)**: Refined **light** chrome as the deliberate inverse of dark mode—navy sidebar brand row with yellow mark, **blue → yellow** main header band, and a header **theme toggle** that sits flush on the yellow band (no separate white chip or sky “rail”).
- **Settings navigation & layout**: Settings live under `**/settings/<slug>`** with redirect from `/settings` to the first section; the old in-page settings sidebar was removed in favor of **nested entries in the main app sidebar** while on settings routes.
- **Settings vs onboarding parity**: **Media Integrations** and **ARR Integrations** reuse the same **card / slot grid** patterns as onboarding; other sections use the same framed, onboarding-style section treatment where appropriate.
- **Integration logo tiles**: Plex/Jellyfin/Emby and Radarr/Sonarr icon wells use a **solid studio navy** (`#1e2430`) in both themes so tiles match the intended dark-blue look instead of washed translucent fills.
- **Brand logo rendering**: Sidebar mark uses a single `**BrandLogo`** path with a `**variant`** switch (blue vs yellow asset) so light and dark placement stay consistent.
- **Dashboard document title**: Browser tab title is now **Placeholdarr** instead of “Placeholdarr Dashboard Next.”
- **Branding type surface**: The TypeScript `**Brand`** union is now **Placeholdarr-only**, matching the shipped Studio product chrome.
- **Semantic / global CSS**: `brandSemanticTheme.ts`, `styles.css`, and Tailwind font-map comments were tightened so new UI prefers `**--brand-*` tokens** over legacy hard-coded slate aliases.

### Fixed

- **Light-mode main header**: The top bar now applies the intended **blue → yellow gradient** in light mode as well (it was only wired for dark mode before, so the strip read as a flat pastel).
- **Settings routing guard**: Settings URL normalization waits on `**settingsPayload`** instead of an empty derived section list, avoiding edge cases where `/settings` would not redirect after load.

### Dependencies

- **SQLAlchemy**: Declared as `**sqlalchemy>=2.0.44`** in `requirements.txt` for consistent installs across environments.

## [0.9.4.1] - 2026-04-21 [HOTFIX]

### Fixed

- **Playback resolution hotfix**: Enhanced playback context resolution by merging path-based info with catalog IDs (TVDB, TMDB, IMDB, Sonarr series, season/episode). This improves detection of real vs placeholder media when file paths differ (e.g., Docker, root changes), reducing unknown/ambiguous playback cases. (see `event_playback.py`)

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

