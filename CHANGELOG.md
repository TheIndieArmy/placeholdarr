# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog,
and this project follows Semantic Versioning while in pre-1.0 stabilization.

## [Unreleased]

## [0.9.22] - 2026-08-31

### Summary

- **Tracearr playback**: Accept Tracearr JSON webhooks (`instance=tracearr`) for stream start on Plex, Jellyfin, and Emby.
- **Per-player playback notifier**: Each media player chooses Tautulli/native webhook or Tracearr.
- **Tracearr on Emby**: Combined with Tracearr, Emby users hear playback without Emby Premiere (native Emby webhooks require Premiere).
- **Tracearr Jellyfin/Emby SSE**: Tracearr SSE on the media server is required so short placeholder plays are not missed between poll cycles.
- **Playback setup modal**: Media cards open a chooser plus setup steps (Settings and onboarding).
- **Tracearr Automations setup**: Instructions use Automations → Start from scratch, When “A stream is first seen”, Then Send Notification.
- **Plex playback copy**: Playback needs Tautulli or Tracearr (not Tautulli only).

### Added

- **Tracearr playback ingest**: `POST /webhook?instance=tracearr` accepts Tracearr `stream_started` JSON (IDs + subtitle S/E) and drives placeholder search. Requires Tracearr v2.2.4+.
- **Tracearr Automations setup**: Playback setup (Settings and onboarding): JSON destination, Start from scratch, When “A stream is first seen”, Then “Send Notification”.
- **Tracearr setup intro**: One shared message (one webhook for every player); full steps on first Tracearr player, shortened path with expandable steps when Tracearr is already saved on any player.
- **Per-player playback notifier**: Settings keys `PLEX_PLAYBACK_NOTIFIER`, `JELLYFIN_PLAYBACK_NOTIFIER`, and `EMBY_PLAYBACK_NOTIFIER` (defaults keep Tautulli/native).
- **Playback setup modal**: Per-player native vs Tracearr chooser with shared Tracearr URL instructions.

### Changed

- **Plex media card copy**: Playback needs Tautulli or Tracearr; card action is Playback setup (not Webhook URL).
- **Security webhook destinations**: Lists Tracearr once when any player uses it; native URLs only for players on native/Tautulli.
- **Playback setup modal**: Save is disabled when the selected notifier already matches settings; Close dismisses without saving.
- **Media Integrations cards**: Remove connection asks for confirmation before clearing URL and credentials (Settings and onboarding).
- **Playback notifier gating**: When a player is set to Tracearr, leftover Tautulli/native webhooks for that player are ignored.
- **Tracearr setup (Jellyfin/Emby)**: Copy states Tracearr SSE on the media server is required.
- **Tracearr setup (Emby)**: Copy notes combined with Tracearr, Emby users hear playback without Emby Premiere.
- **Tracearr webhook notes**: Document Jellyfin/Emby SSE requirement and Emby without Premiere.

## [0.9.21] - 2026-08-31

### Summary

- **Library detail (Movies)**: Redesigned detail page with overview-first layout, meta facts, Arr status tiles, cast, franchise collection strip, and full-width technical details.
- **Library detail (TV)**: Redesigned detail page with episode progress under Seasons & Episodes, enhanced season/episode rows, and Arr status tiles (`files/total` / Downloaded).
- **Detail API**: Movie and series detail payloads add ratings_display, actors, display_status, paths, episode_stats, season fields, per-instance monitored, and collection_members.
- **Collection strip**: Franchise members from the library plus TMDB-only titles when a Collection Sources key is set.
- **Library sort**: Movies gain year and release-type sorts; TV gains premiere and last-aired sorts.
- **Yellow accent buttons**: Active pills and primary actions on brand yellow use dark on-accent text.

### Added

- **Library detail (Movies)**: Redesigned detail page with overview-first layout, meta facts, Arr status tiles, cast, franchise collection strip, and full-width technical details.
- **Library detail (TV)**: Redesigned detail page with episode progress under Seasons & Episodes, enhanced season/episode rows, and Arr status tiles (`files/total` / Downloaded).
- **Detail API**: Movie and series detail payloads add ratings_display, actors, display_status, paths, episode_stats, season fields, per-instance monitored, and collection_members (DB siblings).
- **Collection strip**: Movie detail shows franchise members from the library plus TMDB-only titles when a Collection Sources key is set (downloaded / placeholder / missing / not in library).
- **Collection strip links**: Library siblings open their movie detail pages; titles not in the catalog open TMDB.
- **Detail ratings**: Scores sit in a mini-card row with IMDb / TMDB / Rotten Tomatoes / Metacritic / Trakt icons; TMDB/Trakt/IMDB values round to one decimal.
- **Library sort (Movies)**: **Year (newest/oldest)** matches *arr-style year + title ordering; **Theatrical**, **Digital**, and **Physical** release date sorts.
- **Library sort (TV)**: **Premiere** and **Last aired** (episodes on or before today) newest/oldest options.

### Changed

- **Library shelf cache**: Refetches when movie rows lack theatrical dates; library list fetches bypass browser HTTP cache (`no-store`) so release-type sort does not stick on title A–Z after API upgrades.
- **Library detail back**: Grid stays mounted while viewing an item so returning from movie/TV detail is instant instead of rebuilding the full shelf.
- **Detail Arr tiles (light)**: Placeholdarr / Radarr / Sonarr status cards use light surfaces and dark text like score cards; icon wells stay dark for contrast.
- **Detail Arr tile titles**: Instance names reserve two lines so status labels align across the row.
- **Yellow accent buttons**: Active pills and primary actions on brand yellow use dark slate text (`--brand-fg-on-accent`) across library, settings, collections, modals, and onboarding.
- **What's new modal**: Notices group under version headers; Got it stays pinned at the top; informational releases (no action required) no longer block startup.

### Fixed

- **Detail trailer**: Radarr YouTube trailer ids open YouTube instead of a relative library URL.
- **Detail API errors**: FastAPI validation payloads no longer surface as `[object Object]` in the detail banner.

### Removed

- **Library card style preview page**: `/library/card-styles` and **Compare all styles** removed; style pills on Movies/TV remain.

## [0.9.20] - 2026-08-30

### Summary

- **Playback setup modal**: Media cards open setup steps for Tautulli / Jellyfin / Emby webhooks (Settings and onboarding).
- **Webhook copy confirmation**: Copy buttons show a checkmark for a couple seconds after a successful copy.
- **Onboarding Look and feel**: Status / poster overlay fields live only on Look and feel, not again on Behavior.
- **Activity layout**: Placeholders + Tasks (infinite scroll, filters, series batches, Active searches); Operations removed from Activity.
- **Activity series-add batches**: Burst “Series added” creates collapse into one expandable row.
- **Activity series create batches**: Same-series Created rows within a few minutes group (sync or SeriesAdd).
- **Activity series delete title**: Tombstone bulk-delete rows show the series name once (not duplicated).
- **Activity reason prose**: Long placeholder reasons stay intact instead of snake_case flattening.
- **Errors page removed**: Mock Diagnostics shell dropped; failed jobs stay on Activity → Tasks, live logs on Logs.
- **Errors API removed**: Unused `GET /api/errors` dropped with the Diagnostics shell.
- **Shared placeholder tombstone**: Deleted-instance Placeholder rows clear path; FS scan will not revive them when a sibling still owns the file.
- **Series stats vs full sync**: Stats refresh waits until full sync ends, then retries with backoff.
- **Full sync row locks**: Series sync fetches Sonarr episode payloads first, then commits after each show.
- **Full sync autoflush**: Season/episode lookups no longer flush a pending series UPDATE.
- **Full sync deadlock retry**: Scheduled full sync retries once after a Postgres deadlock.
- **Library shelf lock wait**: Library payload queries abort after 30s instead of holding a pool slot.
- **Media Integrations configure**: Connect and Configure open an ARR-style modal (Cancel / Test / Save).
- **Collection Sources**: TMDB, Trakt, and Tautulli collection APIs move to Settings → Collection Sources.
- **Media/ARR Test button**: Closing and reopening Configure clears the success check; Save still works when details are already complete.
- **Collection poster titles**: Long titles on collection posters scale down so more of the name stays readable.

### Added

- **Playback setup modal**: Per-player Tautulli / Jellyfin / Emby webhook instructions from media cards.
- **Webhook copy confirmation**: Copy controls swap to a check icon briefly after copying.
- **Activity layout**: Placeholders and Tasks under Activity (filters, day groups, Active searches on Tasks).
- **Activity scroll**: Placeholders append older history on scroll (cursor `before_time` / `before_id`).
- **Activity series-add batches**: Consecutive “Series added” episode creates group into one expandable row.
- **Activity series create batches**: Same-series Created rows within ~5 minutes group regardless of reason (lite/full sync, SeriesAdd, etc.).
- **Activity Active searches**: Tasks shows Placeholdarr-monitored titles above Scheduled (`GET /api/activity/active-searches`).
- **Collection Sources**: Settings sidebar page (after ARR Integrations) for TMDB API key, Trakt Client ID, and Tautulli URL / API key used by collection list sources.

### Changed

- **Plex media card copy**: Playback needs Tautulli; card action is Playback setup (not Webhook URL).
- **Onboarding Behavior step**: Look and feel fields (status updates, projection, poster overlay) no longer appear under Behavior.
- **Activity feed APIs**: `/api/activity/placeholders` and `/api/activity/operations` return `{ items, has_more, next_before_time, next_before_id }`.
- **Activity promoted**: Proposed Placeholders / Tasks replace the old Activity tabs; `/activity/proposed/*` and `/activity/operations` redirect.
- **Activity series delete title**: Series tombstone bulk-delete rows use a single series title; multi-episode bursts expand like create batches.
- **Activity reason prose**: Placeholder Created/Deleted reasons that are already sentences are not snake_case-humanized.
- **Media Integrations configure**: Connect and Configure open a modal with Cancel, Test, and Save instead of expanding a form under the cards.
- **Collection poster titles**: Poster title fallback uses dynamic font sizing so longer names fit better.

### Removed

- **Errors page**: Sidebar Diagnostics shell removed; `/errors` redirects to Logs. Failed jobs remain on Activity → Tasks.
- **Errors API**: Unused `GET /api/errors` removed after the Diagnostics shell was dropped.

### Fixed

- **Shared placeholder tombstone**: Tombstoned Placeholder rows clear `path`; FS scan prefers active siblings and will not revive deleted-instance rows.
- **Refresh all placeholders contrast**: Accent button uses dark on-accent text instead of white on yellow.
- **Activity series delete title**: Tombstone bulk-delete no longer shows `Series • Series` when both title fields are the series name.
- **Activity reason prose**: Long tombstone reasons keep punctuation instead of flattening to spaced words.
- **Media/ARR Test button**: Closing and reopening Configure clears the success check; Save still works when connection details are already complete.
- **Series stats vs full sync**: Episode-stats refresh skips while a full sync is running, fails lock waits in 3s, and backs off up to 60s instead of retrying the same row every couple of seconds.
- **Full sync row locks**: Sonarr full sync fetches episode payloads first, then commits after each series so other queries are not blocked for the whole library pass.
- **Full sync autoflush**: Series/season/episode lookups use `no_autoflush` so a pending series UPDATE cannot deadlock with another writer.
- **Full sync deadlock retry**: Scheduled full sync retries the instance once after a Postgres deadlock.
- **Library shelf lock wait**: `/api/library` sets a 30s lock timeout so a blocked shelf load cannot pin a pool connection for tens of minutes.

## [0.9.19] - 2026-08-26

### Summary

- **Add to Radarr/Sonarr**: Chunks of 20, then **library poll** after a silent *arr import.
- **Add modal progress**: Live per-title status in the add modal.
- **Add tags**: Chips (Enter to add, spaces become dashes); a failed tag no longer aborts the add.
- **Webhook ingest**: Persist is serialized so import-list floods cannot exhaust the DB pool.
- **Seasonal Run now**: Out-of-window runs follow keep / empty / delete instead of filling the collection.
- **Seasonal delete**: Dormant recipes can delete Placeholdarr-owned Plex collections (Sets delete managed children).
- **Missing from catalog**: Preview and recipe list copy (the check is catalog membership).
- **Sidebar**: Selected-tab and hover styling.
- **MDBList ids**: Public JSON `id` is treated as TMDB id.
- **ARR lookup**: Tries TMDB/TVDB/IMDb then a title search.

### Added

- **Add modal progress**: After submit, the form is replaced by a per-title list that flips **Adding…** to a green **Added** (or an error) as each title is found.
- **Seasonal window: delete**: When dormant, a recipe can **delete** Placeholdarr-owned Plex collections (in addition to keep as-is or empty). Collection Sets delete the child collections they manage. Unlabeled same-title shelves are left alone.

### Changed

- **Add to Radarr/Sonarr**: Import chunks of 20. *arr often never returns `movie/import` / `series/import`, so Placeholdarr waits briefly then **polls the library** until titles appear.
- **Placeholder ingest**: Already-in-*arr and reconciled titles still enqueue placeholder ingest.
- **Add tags**: Tags are chips (Enter to add, spaces become dashes); a failed tag no longer aborts the add. API accepts `tags: string[]` (legacy `tag` still works).
- **Webhook ingest**: Persist is serialized (`WEBHOOK_INGEST_CONCURRENCY`, default 1) so import-list `MovieAdded` floods cannot exhaust the DB pool.
- **Collections missing list**: Preview and recipe list describe titles **missing from catalog**, not “not in Radarr/Sonarr” (the check is catalog membership).
- **Sidebar**: New selected-tab and hover styling.
- **Manual Run now (seasonal)**: Out-of-window Run now follows the same keep / empty / delete policy as scheduled sync instead of creating or filling the collection. Last run shows **Kept**, **Cleared**, or **Removed (out of window)**.

### Fixed

- **MDBList ids**: Public JSON `id` is treated as TMDB id.
- **ARR lookup**: Radarr/Sonarr lookup tries TMDB/TVDB/IMDb then a title search (so a bad or missing id does not fail a title Radarr can find by name).

## [0.9.18] - 2026-08-24

### Summary

Collections (Beta) expands with unified **TMDB/Trakt** source cards plus **Tautulli** and **\*arr tags**, **Collection Sets** (one config → many Plex shelves), **ownership-safe** Plex sync (summary footer + ratingKeys), **Validate** for pasted list/page URLs, and portable **export/import** of recipes between installs.

### Action required

**Reconnect existing Collections:** Placeholdarr now tracks Plex collection ownership internally, rather than matching by name. Open each affected recipe and **save** it: you will be prompted to **adopt** the matching collection or rename the recipe. Until you do, scheduled runs for that recipe will fail and leave the Plex collection unchanged. Prefer adopt for collections Placeholdarr already created. If you built the collection in Plex or another tool, rename instead so Placeholdarr does not claim it. Adopting syncs the collection to the recipe, so non-matching items will be removed.

### Added

- **Collection sources**: Unified **TMDB** and **Trakt** source cards with cascading subtypes; Tautulli most popular / most watched (optional `TAUTULLI_URL` + `TAUTULLI_API_KEY`); Radarr/Sonarr tag mirrors (`arr_tag`). Trakt Settings copy notes that creating a Trakt API Client ID currently requires **Trakt VIP**.
- **Collection Sets**: One config fans out to many Plex collections by **genre**, **decade**, **content rating** (age certification — not critic scores), **\*arr tag**, or **release timing** (Upcoming / released this week|month|year|decade, with a shared release-date basis like recipe filters). Include/exclude value selection, title patterns, live preview, and cleanup of stale child collections this set previously managed.
- **Collection ownership**: Synced collections are tracked behind the scenes (labels, summary footer **Managed by Placeholdarr.**, and ratingKeys). Placeholdarr only edits collections it owns. If a same-named collection already exists in a selected library, save asks you to **rename** or **adopt**. Prefer adopt for collections Placeholdarr already created; rename if the shelf was built in Plex or another tool. Adopting syncs membership to the recipe, so non-matching items may be removed.
- **Collection source Validate**: URL/list source cards (TMDB Page, MDBList, Trakt list, AniList, StevenLu, legacy TMDB link types) gain a **Validate** button that resolves the pasted link and shows the title/kind or an error before you run preview. If the collection title is still blank, a successful validate can fill it in.
- **Collection export/import**: Export selected recipes (or Collection Sets) to a portable JSON file, or import a bundle into this install. Runtime fields (ids, last-run stats, Plex ratingKeys) are stripped on export; import rebinds to your Plex libraries and creates new recipes.

### Fixed

- **Tautulli collection source**: Home-stats rows that only expose modern `plex://` (or episode-level agent) guids no longer yield an empty candidate list. Placeholdarr resolves TMDB/IMDb/TVDB ids via Tautulli `get_metadata` and strips a trailing `(Placeholder)` from titles.
- **Collection same-title twins**: Sync no longer creates a second Plex collection with the same name in a library (which produced 0-item counts and linked deletes). Conflicts require rename or explicit adopt.

## [0.9.17] - 2026-08-18

### Summary

Collections (Beta) gains new source types, a unified **TMDB Page** card, explicit **source-level sort**, a TV-specific **Was airing during** year filter, an **unsaved recipe** guard, improved **Plex path refresh** for Docker, and a better **preview** and **Missing-from-ARR** workflow.

### Added

- **Collection sources**: StevenLu (default popular-movies JSON or a custom URL), AniList public user anime lists (GraphQL, throttled), and TMDB person / company / keyword / collection. Paste a TMDB page URL (or numeric id); no in-app search picker.
- **TMDB Page source card**: One unified source block replaces the separate TMDB Person / Company / Keyword / Collection cards. Paste any TMDB page URL and the card detects the type. Company and keyword pages get a **Sort from TMDB** dropdown that controls which TMDB Discover order the fetch uses (popularity, rating, release date, title). Legacy recipes using the older separate types still work.
- **Collection year filter (TV)**: New **Was airing during** mode matches shows whose first–last air years overlap the window (unknown last air = still running). Movies and the default **Premiere year** still use release / first-air year.
- **Unsaved collection recipe prompt**: Leaving the recipe editor (Back, Cancel, Library, or another dashboard page) shows an in-app confirm modal. Closing the browser tab still uses the browser's native leave prompt (not available in all embedded browsers).

### Changed

- **Collection title sort**: Arrange by Title (A–Z) ignores leading *a* / *an* / *the*, matching Library A–Z.
- **Plex collection order**: Synced collections use Plex custom sort and item order from the recipe (Plex previously kept default release-date order, which looked random vs preview).
- **Collection preview**: Catalog sample shows up to 200 selected titles with file vs placeholder outlines. Multi-library recipes mark which Plex libraries each poster is in (filter chips dim the rest). Missing-from-ARR uses the same Arrange sort; **Select first N** matches the recipe max (capped at the add batch).
- **Missing-from-ARR add**: Lookups stay per title; Radarr/Sonarr adds go through `movie/import` / `series/import` (chunks of 100) with a **90s** timeout. Timeouts say the *arr may still add the title. Result titles include year.
- **Plex path refresh**: Path-scoped library scans rewrite Placeholdarr folders onto Plex's library locations (cached in memory from `/library/sections`, filled on connection test or first refresh). Docker mounts like `/placeholdarr/movies` no longer get skipped as unknown paths.
- **TMDB keyword/company sort**: Source fetches now send `sort_by` to TMDB Discover (default `popularity.desc`). Previously this was ignored, so the pool of titles could miss popular items and Arrange by popularity only reordered whatever unsorted pages came back.
- **TMDB source cards simplified**: The Add Source menu now shows **TMDB Page** instead of 4 separate link-type entries. Existing recipes are unaffected.

## [0.9.16] - 2026-08-14

### Action required

**Webhook URLs must be updated.** `POST /webhook` now requires `?apikey=` unless `AUTH_MODE=disabled`. Existing Radarr, Sonarr, Tautulli, Jellyfin, and Emby notification URLs **without** that query parameter will be rejected.

1. Open Settings → Security (or the in-app **What's new** prompt).
2. Use **Webhook URLs** to copy each destination (API key is masked until you reveal).
3. Paste the URL into that service and save.
4. Regenerating the key invalidates every previous URL until you recopy.

Follows **[#61](https://github.com/TheIndieArmy/placeholdarr/pull/61)**.

### Summary

This release makes **dashboard-authenticated webhooks** work: *arr and playback sources are not browser sessions, so they send a dedicated webhook API key. Setup and Security copy buttons include it; the key is hidden in the UI until you reveal. Existing installs get an **Action required** What's new notice; new setup stamps the current version so upgrade-only prompts are skipped.

Collections (Beta) gains **Missing from ARR** add-to-*arr, **rating filter providers**, **sort by rating**, and **multi-library recipes**. Dashboard HTML is no longer cached so a refresh actually loads this UI.

### Security

- **Webhook API key (breaking):** `POST /webhook` requires `?apikey=` (skipped only when `AUTH_MODE=disabled`). View, copy, and regenerate under Settings → Security. Cookie/CSRF login does not replace this key.
- **Masked webhook URLs:** Setup modals and the Security **Webhook URLs** modal hide `apikey=` until Reveal; Copy still places the full URL on the clipboard.

### Added

- **Webhook API key**: `AUTH_WEBHOOK_API_KEY` / Settings payload `webhook_api_key`; `GET/POST /api/auth/webhook-key`. Copy URLs in onboarding, ARR, and playback setup append `&apikey=`.
- **What's new notices**: Tracks `LAST_SEEN_APP_VERSION` and dismissed notice ids. Upgrade messages after a version jump (including skipped releases). Sidebar version chip reopens the catalog. Webhook recopy prompt, then Collections (Beta) intro.
- **Add missing list titles to Radarr/Sonarr**: Live preview **Missing from ARR** (source candidates not in the Placeholdarr catalog, after filters that can run off the list). Year, TMDB genre/language/date, and TMDB/MDBList scores when present. ARR instance/profile/monitored/quality **include** rules treat missing titles as a miss; **exclude** rules treat them as a match. A notice lists filters that cannot be fully applied (certification, studio, non-TMDB ratings, non-premiere release windows). Multi-select add with monitor, search (default off), tag, quality profile, root folder, and one or more instances. Recipe list shows last-run **N not in Radarr/Sonarr** and **+N new**.
- **Rating filter sources**: Movie recipes choose Radarr provider (`imdb` default, `tmdb`, `trakt`, `metacritic`, `rottenTomatoes`) with native scales (0–10 or 0–100); TV recipes use Sonarr’s flat Skyhook score. Optional `min_votes`. Legacy movie rules without `provider` keep the previous best-effort fallback.
- **Sort by rating**: Arrange option `rating` with optional `sort_provider` (movies); missing scores sort last. Independent of the rating filter.
- **Multi-library recipes**: `plex_section_ids` JSON (Alembic `0024`); editor multi-select (same movie/TV type); preview and last-run show per-library in-library counts. `plex_section_id` remains the first target for compatibility. Same collection title is created/updated in each selected library (membership is per library).

### Changed

- **Rating filter defaults**: New movie rating filters default to **IMDb** on a 0–10 scale; editing a legacy rating rule stamps `provider: imdb` when saved from the UI.
- **Dashboard HTML is not cached**: SPA `index.html` is served with `Cache-Control: no-store` so a refresh on `/library`, `/collections`, or Settings loads the current UI. After reconnect, an already-open tab reloads if the JS bundle on disk is newer.
- **Playback webhooks**: Only **Playback Started** is documented and processed. Stop/Resume are not listed in setup.
- **Webhook URL inventory**: Settings → Security **Webhook URLs** opens a modal listing every connected Radarr/Sonarr/Tautulli/Jellyfin/Emby URL for recopy after key rotation.

### Fixed

- **Seasonal window dates**: Month/day pickers clamp invalid days before save; backend accepts `MM-DD` and `YYYY-MM-DD` and reports which of `start`/`end` failed validation.

## [0.9.15] - 2026-08-13

### Summary

This release adds **Plex Collections automation (Beta)**: saved recipes pull titles from TMDB, MDBList, Trakt, or your Placeholdarr catalog, filter and sort them, then sync membership into a Plex collection on a schedule. A new **Collections** dashboard tab provides a block-based editor with **live preview**, per-title **explain**, manual runs, and integration with Activity → Tasks.

It also closes **[#58](https://github.com/TheIndieArmy/placeholdarr/issues/58)** with **dashboard authentication on by default** (stronger than a bare *arr-style login): Argon2id passwords, signed sessions, CSRF, rate limits, and optional reverse-proxy **forward auth**. ARR instance **API keys are redacted** in settings responses. Media Integrations cards gain an **enable/pause toggle** while keeping **Remove connection**.

Also included: **favicon** assets, a shared **toggle switch** component, and **TMDB attribution** in Settings.

### Security

- **Dashboard auth (default on):** first-run / post-upgrade admin account with Argon2id password hashing, signed session cookies (14-day lifetime), CSRF protection on mutating `/api` calls, and login rate limiting.
- **Auth modes:** `builtin` (default), `forward_auth` (trust `Remote-User` / `X-Forwarded-User` only from `AUTH_TRUSTED_PROXIES`), and `disabled` (explicit opt-out; documented as unsafe if the port is exposed).
- **Secret redaction:** `ARR_INSTANCES_JSON` no longer returns plaintext `api_key` values to the browser; blank keys retain the saved server value on save (same UX as other secret fields).
- **Docs:** README Security section and docker-compose port warning for trusted-network / reverse-proxy deployments.
- **Fixes [#58](https://github.com/TheIndieArmy/placeholdarr/issues/58):** unauthenticated dashboard/API credential exposure.

### Collections — overview

Collections let you maintain Plex collections from rules instead of hand-picking titles. Each recipe targets a **Plex movie or TV library**, defines where candidate titles come from, which metadata rules they must pass, how results are ordered and capped, and optional **include/exclude pins**. On each run, Placeholdarr matches candidates to catalog rows, resolves Plex `ratingKey`s, and **creates or updates** the collection (add/remove to match the recipe). Only titles **already in that Plex library** are added; unmatched rows are reported as unresolved.

Typical use: a “Trending this week” or “Recently aired” collection in a placeholder library that stays in sync as your catalog and external lists change.

**Collections feature set:**

- **Sources**: TMDB trending (day/week), popular, upcoming, discover (genres, year, providers, ratings), public TMDB lists; public MDBList lists (no API key); public Trakt user lists; full Placeholdarr catalog pool. Multiple sources union and dedupe by id.
- **Release window**: *Has been released*, *not yet released*, *released in the past*, and *releasing in the next* — with a **based on** date: movies use theatrical / digital / physical release; TV uses series premiere, latest aired episode, or latest season premiere.
- **Filter logic**: Simple mode — AND within a group, OR between groups. **Advanced filtering** — nested AND/OR groups up to three levels deep; single-group recipes flatten to a top-level AND when switching modes.
- **Arrange**: Sort by popularity, release date, latest aired, or title; item limit (up to 500); include pins survive the limit, exclude pins always drop.
- **Pins**: Force-include or force-exclude specific catalog titles via library search (TMDB/TVDB/IMDB ids).
- **Live preview**: Staged pipeline counts (candidates → catalog match → filters → pins → selected → in library) plus a sample poster grid; debounced as you edit.
- **Explain**: Per-title debugger with pass/fail/skip at each stage and a recursive filter verdict tree.
- **Scheduling**: Global `collections_sync` interval (Settings) plus per-recipe override (hourly through weekly). Seasonal **active windows** (`MM-DD` ranges, including wrap-around) with *keep* or *clear collection when inactive*.
- **Tasks**: `collections_sync` in Activity → Tasks (Run now, history); per-recipe Run now from the list.
- **Plex required**: When Plex is not configured or unreachable, Collections shows a banner with a Settings shortcut; **New Collection** and **Run now** are disabled. Existing recipes remain listed.

### Added

- **Dashboard authentication**: Admin setup/login UI, `/api/auth/*` routes, session middleware, CSRF + rate limits, Settings → Security (`AUTH_MODE`, trusted proxies, change password, logout).
- **Collections builder (end-to-end)**: Recipe CRUD API, rule engine, Plex collection sync, TMDB/MDBList/Trakt list clients, `collection_recipe` DB table (Alembic `0022`), and Collections dashboard tab with list view + block editor pipeline (Sources → Filters → Pins → Arrange).
- **Collections Beta label**: Sidebar nav and Collections page header show a Beta chip while the feature stabilizes.
- **Collections API**: `GET/POST /api/collections`, `GET/PUT/DELETE /api/collections/{id}`, toggle, manual run, `plex-sections`, `tmdb-meta`, `builder-meta`, `POST /api/collections/preview`, and `POST /api/collections/explain`.
- **Source blocks**: `tmdb_trending`, `tmdb_popular`, `tmdb_upcoming`, `tmdb_discover`, `tmdb_list`, `mdblist`, `trakt_list`, and `catalog` with per-source limits and multi-source union/dedupe.
- **Filter blocks**: Genre, year, certification, studio/network, monitored, quality profile, original language, instance, release window, and rating — backed by ARR metadata and catalog fields.
- **Advanced filter nesting**: Boolean AND/OR filter trees (depth ≤ 3) with simple/advanced editor modes, recursive explain verdict tree, and validation in the engine.
- **Release window filters**: `has_released`, `not_yet_released`, `within_past`, and `within_next`; movie bases (theatrical, digital, physical); TV bases (series premiere, latest aired episode, latest season premiere) with air-date aggregation for ongoing shows.
- **Pins**: Include (bypass sources/filters, survive limit) and exclude (always removed) with catalog typeahead from the shared library cache.
- **Live preview & explain**: Debounced preview rail with pipeline stage counts, sample posters, and per-title stage-by-stage explain popover.
- **Collections scheduling**: `COLLECTIONS_SYNC_INTERVAL_HOURS` setting; `collections_sync` scheduled task; per-recipe `run_interval_hours` with scheduler tick at the minimum enabled override (`0023` migration).
- **Seasonal active windows**: Per-recipe `active_window` (`start`/`end` as `MM-DD`, `when_inactive`: keep or clear); dormant badge in UI; clear runs once when leaving the window.
- **Plex collection sync** (`plex_collections.py`): Section provider index (cached), ratingKey resolution, create-or-diff membership, last-run summary on each recipe.
- **Settings**: `TMDB_API_KEY`, `TRAKT_CLIENT_ID`, and `COLLECTIONS_SYNC_INTERVAL_HOURS` in the Settings UI; TMDB attribution block under the TMDB API key field.
- **Collections UI theming**: `collectionTheme.ts` with light/dark tokens; responsive editor layout with preview rail.
- **Shared `ToggleSwitch`**: Reusable toggle used in Collections and across Settings/other dashboard controls.
- **Favicon**: `favicon.ico`, 16×16 and 32×32 PNGs, and `apple-touch-icon.png`; served from the dashboard static routes; link tags in `index.html`.
- **Dependencies**: `argon2-cffi`, `itsdangerous`, and `httpx` for auth sessions and tests.

### Changed

- **Collections editor UX**: Advanced filtering toggle; simple mode stores single AND groups as flat rule lists; converting to advanced flattens redundant OR/AND wrappers so rules sit at top-level AND when appropriate.
- **Release window defaults**: New movie release-window filters default to **theatrical** release; dropdown order is theatrical → digital → physical.
- **Dashboard navigation**: New Collections tab; library catalog cache passed through for pin search and explain typeahead.
- **Task scheduler**: Collections sync job respects per-recipe interval overrides; recipe CRUD/toggle refreshes the collections schedule.
- **Media Integrations cards**: Enable/pause toggle on connected Plex/Jellyfin/Emby cards (Settings and onboarding) without opening Configure; **Remove connection** still clears card fields and returns to Connect.
- **Collections without Plex**: Banner + Settings shortcut; New Collection and Run now disabled until Plex libraries are available.

### Fixed

- **Collections preview validation**: Engine accepts movie release-window bases (theatrical, digital, physical) for live preview and saved recipes.
- **Advanced filter toggle**: Empty or linear filter trees no longer grey out the Advanced filtering switch incorrectly.
- **Auth mode dropdown**: `AUTH_MODE` options use `{value, label}` objects so the Security settings select is not blank.
- **Collections nav label**: Beta chip no longer truncates “Collections” in the sidebar.

## [0.9.14] - 2026-06-09

### Summary

This release makes the dashboard **feel live** without hammering the API: **server-sent events** push library version changes and startup-sync status, the **Logs** tab streams new lines in real time, and background polling backs off when SSE is connected. **Library poster requests** stay read-only on the hot path — `poster-grid.jpg` is materialized in a background thread instead of during browser loads.

Large *arr libraries also get more headroom: bulk Radarr/Sonarr HTTP reads now use a **120s timeout** (was 30s) so full sync and catalog fetches are less likely to fail on slow user-share I/O.

### Added

- **Live log streaming**: In-process ring buffer captures formatted log lines; `GET /api/logs` supports `since_id` for incremental tailing and level filtering; `GET /api/logs/stream` pushes new lines over SSE. Cold start falls back to the on-disk log file until the buffer has entries.
- **Dashboard SSE (`GET /api/events`)**: Pushes `startup_sync_complete`, `library_version` (movies/series counters), and periodic pings so the UI can react without polling every tab on a fixed interval.
- **Health and readiness probes**: `GET /api/health` (liveness, no DB) and `GET /api/ready` (includes startup sync gate) for browser reconnect logic and startup UI.
- **Frontend data hooks**: Extracted `useLogsStream`, `useDashboardEvents`, `useApiHealthCheck`, `useActivityFeed`, `useActivityTasks`, `useCalendarData`, `useErrorsFeed`, `useSetupStatusPoll`, and `useStartupReadyPoll` so `App.tsx` owns routing/layout and each tab manages its own fetch/stream lifecycle.
- **Library poster grid backfill**: Background thread creates missing `poster-grid.jpg` beside existing `poster.jpg` for placeholder rows; `GET /api/library/poster` serves pre-materialized files only (no art download on request).

### Changed

- **Dashboard polling strategy**: When SSE is connected, library version checks skip the 60s poll loop (events drive refetch); health and startup-ready probes use exponential backoff and idle when the event stream is up. Tab visibility still triggers catch-up refreshes.
- **Library poster resolution**: Poster path lookup and cache-busting tokens live in `services/library_poster_paths.py`; list/grid `poster_url` values point at the dedicated poster API with stable cache tokens.
- **ARR HTTP timeout**: Shared `ARR_HTTP_TIMEOUT_SECONDS` raised to **120** for bulk movie/series catalog reads, queue-monitor ARR calls, and command polling — reduces read timeouts on large libraries over mergerfs/user shares.

### Fixed

- **CI / Docker frontend build**: `.gitignore` `logs/` no longer ignores `frontend/src/logs/`; `useLogsStream.ts` is tracked so Vite can resolve the import in release builds.
- **TypeScript build**: Setup wizard null-guard for settings payload, Status Updates behavior-wizard section typing, status-messages save-flow scope promise resolution, and minor unused-import / narrowing fixes in library card modules.

## [0.9.13] - 2026-05-29

### Summary

This release makes **large Movies and TV libraries usable in the dashboard**: shelves load on demand, stay cached until the catalog version changes, and no longer block app startup while episode stats backfill runs. You can **sort** by title, year, when a title was added, or last update; header search matches **titles only** for a faster, clearer jump list.

**Spotlight** cards get a cleaner caption (year above title, aligned posters). Changing **calendar lookahead** or specials policy now refreshes **both** movie and TV shelves so Future/Missing filters stay accurate.

### Added

- **Library performance**: Materialized per-series episode stats (`episode_total`, files, placeholders, missing, future) refresh on sync and placeholder changes instead of aggregating all episodes on every `/api/library` request. Library catalog version counters power `GET /api/library/version` and conditional `If-None-Match` responses (304 when unchanged).
- **Per-shelf library cache**: Movies and TV shelves cache independently in the dashboard so switching shelves can show the last loaded catalog immediately when the shelf version has not changed.
- **Library panel extraction**: `LibraryPanel` component for Movies/TV shelves with alphabet sections and card style controls. Removed the 1000-item API cap (full shelf load up to 50,000 rows).
- **Library sort (Movies / TV)**: Toolbar sort control with title (A–Z / Z–A), year, **Added** (newest / oldest), and recently updated. Movies and TV remember their own sort in the browser session. Title sorts keep A–Z section headers and the letter jump rail; other sorts use a flat list.
- **Library catalog timestamps**: `/api/library` rows include `created_at` and `updated_at` (ISO) so the dashboard can sort by when Placeholdarr first indexed a title and when it last changed.

### Changed

- **Series stats backfill no longer blocks HTTP startup**: Materialized episode stats backfill runs in a background thread after DB init so the dashboard can accept requests immediately. Library reads use a bulk SQL aggregation for series that are not backfilled yet, so the TV shelf can load from existing catalog data while backfill continues.
- **Library load on navigation**: Movies and TV shelves fetch when you open that page (or on hard refresh), not on the global 5s dashboard poll. Returning to a shelf uses the in-memory cache when `movies_version` / `series_version` are unchanged. Tab focus and a 60s version check call `GET /api/library/version` and refetch the catalog body only when a version counter changes; on the Library tab the 5s loop is limited to `GET /api/settings/status`. The cache is invalidated after sync tasks and settings saves that affect the catalog.
- **`GET /api/library`**: Returns `total` and `version` alongside `items`; TV loads read precomputed series stats instead of scanning every episode row.
- **Header library search**: Matches **titles only** (movies and series names). Shelves always load the summary catalog (no full overview payload while typing in search).
- **Spotlight library cards**: Year is centered above the title; the Film/Series line under the title is removed. Titles use the same shrink-to-fit band as Stack cards so caption height is fixed and posters align across a row.
- **Added sort semantics**: **Added** uses Placeholdarr `created_at` at full timestamp resolution, with database `item_id` as a tiebreaker when two titles share the same second. Merged standard/4K rows use the earliest `created_at` across instances.

### Fixed

- **Added sort on dual-instance titles**: Merged movie/series grid rows now sort by the earliest Placeholdarr insert time across Radarr/Sonarr instances, not only the canonical instance row.
- **Library version after lookahead settings**: Changing calendar lookahead or include-specials policy now bumps both movie and series catalog versions so Movies filters (Future/Missing) refetch instead of serving a cached shelf with stale flags.
- **Stats refresh under lock contention**: When series stats refresh cannot acquire the advisory lock, pending series updates and shelf version bumps are queued and retried instead of being dropped after commit.

## [0.9.12] - 2026-05-28

### Summary

This release makes the **Library** the center of the dashboard: browse movies and TV with new card layouts, optional list view, and clearer posters while placeholder art work runs in the background. You can **refresh a single title** from its detail page without kicking off a full-library sync, and **full/lite sync runs** now show step-by-step progress in Activity so long jobs are easier to follow.

**Placeholder art** is split from metadata refresh. Choose how posters look in Plex (raw, grayscale, banner, or badge); art files are written beside placeholders and players pick them up after a library scan. **Status message and overlay changes** can apply immediately or on the next sync, with dedicated task rows when you choose Apply now.

If you run large libraries, you should see fewer redundant poster rewrites, full-sync tasks that actually finish when art batches complete, and settings that survive restarts as expected.

### Added

- **Library card layouts and controls**: Five grid styles (Polaroid, Framed, Reveal, Spotlight, Stack) plus list view, adjustable card size, and a **Card styles** preview page. Library opens at **Movies** by default after setup; Movies and TV use separate shelves with saved filters.
- **Library poster serving**: Dashboard grids prefer raw catalog art (`poster-grid.jpg`) when composited overlay posters are enabled, so library tiles stay clean while Plex/Jellyfin/Emby keep using `poster.jpg`.
- **Library entity reconcile (scan & refresh)**: Movie, series, and episode detail **Refresh placeholder** enqueues a forced `entity_reconcile` job: Radarr/Sonarr **Refresh** (with command polling), syncs that title from *arr, then runs scoped placeholder truth, determination, materialization, NFO/art sidecars, path-scoped media-server refresh, and per-item player metadata — without requiring an existing placeholder or triggering a full-library Plex `force=1` scan. The UI polls `GET /api/library/reconcile-jobs/{job_id}` and shows live `step_label` text beside the button.
- **Unified placeholder refresh intent and tasking**: Added `PLACEHOLDER_REFRESH_PENDING` (JSON) to merge metadata/art/template refresh intent across settings and message saves, with `future`/`next_full_sync`/`now` apply-scope handling routed through one backend entry point.
- **Manual and scoped placeholder refresh actions**: Manual `placeholder_refresh` task runs (`metadata`, `art`, or both) plus library-scoped POST actions for movie/series/episode detail views to queue targeted refreshes without forcing a full sync.
- **Placeholder art refresh (decoupled from NFO)**: Batched `placeholder_art_refresh` jobs write local `poster.jpg`, `folder.jpg`, `seasonNN-poster.jpg`, and episode `*-thumb.jpg` beside placeholders. Overlay mode controls compositing vs raw download; files are still written when overlay is off. Full sync and overlay setting changes queue a bulk backfill; lite sync scopes art to touched rows; materialization writes art inline on create. When the last bulk batch finishes, Plex TV/movie libraries refresh with forced metadata (`force=1`); Jellyfin and Emby run library scans.
- **Placeholder poster overlay setting**: `off`, grayscale, top banner, or corner Placeholdarr badge — downloads poster/still art, composites the chosen treatment, and writes local JPEGs beside placeholders.
- **TV season poster art for placeholders**: Sonarr sync requests `includeSeasonImages` and stores per-season `remote_poster` URLs. Art refresh writes Sonarr-style `seasonNN-poster.jpg` (and `season-specials-poster.jpg`) at the series root, with series-poster fallback when a season has no dedicated image.
- **Full sync phased task runs** (`services/task_run_phases.py`): Full sync records explicit phases — ARR catalog sync, filesystem scan, status determination, placeholder materialization, calendar status, art refresh, and metadata refresh (when template backfill is pending) — with per-phase timings and metrics in `scheduled_task_run.summary`. The task row stays **WORKING** until art (and any NFO backfill) jobs finish, then closes with a single wall-clock end time.
- **Tasks UI — expandable phase detail**: Queue/history rows expand to show each phase with start/end times, duration, status badge, and metrics (including art batch `N / M` and poster/still counts). In-progress runs show a live elapsed duration; finished runs can show wall-clock duration when art ran after the main sync steps.
- **Filesystem scan phase metrics**: Show files walked and placeholder media files found (not only “new paths indexed”), with a human **Status** instead of raw `ok`.
- **ARR catalog sync phase metrics**: Radarr rows show movie counts only; Sonarr rows show series/episode counts only (no misleading zeroes for the other type).
- **Interrupted task runs**: On app restart, any `scheduled_task_run` left `working` is marked **failed** (`interrupted_by_restart`) so manual sync is not blocked. `POST /api/tasks/abandon` abandons stuck runs without another restart.
- **Activity — Tasks, Operations, Placeholders**: Activity splits into three sidebar pages (Placeholders default, Tasks, Operations). Tasks shows scheduled full/lite sync (defaults weekly / 12h), run history with startup/manual/scheduled triggers, and Run now confirmations. Lite sync copy notes it includes calendar date refresh and Coming Soon updates. Operations is the live event feed without scheduled sync noise.
- **Poster overlay style previews**: Settings → Status Updates includes an expandable **Overlay style examples** panel (sample TMDB poster with grayscale, top banner, and corner-badge treatments) so you can compare modes before triggering refresh.
- **Onboarding — Look and feel**: New wizard step for placeholder status updates, projection mode, and poster overlay mode (with the same overlay previews). Defaults: status updates **All**, project status into **Both**.

### Changed

- **Library entity reconcile · *arr step**: Reconcile now triggers only Radarr/Sonarr **Refresh** (`RefreshMovie` / `RefreshSeries`), matching the in-app entity refresh button. The previous explicit **Rescan** command is no longer queued (that matched **Refresh & Scan** and forced disk I/O even when *arr is set to rescan manually or never). If your instance is configured to rescan after refresh, *arr may still run a scan on its own schedule.
- **Library entity reconcile logs**: Reconcile jobs now log with the title in view (`Library reconcile · Movie · …`) at each step, and scoped determination/materialization lines include the same subject when triggered from library refresh.
- **Full sync follow-up policy**: Full sync now consumes unified pending placeholder refresh intent instead of always forcing art refresh plus separate metadata flag paths, reducing duplicate backfill trees and keeping follow-up behavior consistent.
- **NFO sidecars are metadata-only**: Movie, TV show, and episode NFOs no longer include `<thumb>`, `<art>`, `<poster>`, `<fanart>`, or `<banner>` tags. Plex/Jellyfin/Emby pick up local JPEGs via library refresh and Sonarr-style filenames, not NFO art references. Run **Metadata Refresh** only when status templates or text fields change; **Full sync** queues art reconcile separately.
- **Placeholder poster overlay setting**: Description updated — local art files are always written when remote URLs exist; overlay mode only changes treatment (grayscale/banner/badge vs raw download).
- **Full sync pipeline layout**: Calendar date refresh, filesystem scan, determination, materialization, and calendar/orphan cleanup run as separate tracked phases instead of one opaque “self-healing” block. Template/NFO backfill on full sync is optional (pending flag only); art backfill is always queued when active placeholders exist.
- **Art skip / regen stability**: `.poster-overlay.json` tracks per-artifact `source_url` and `source_kind` (still vs fanart, season vs series fallback) so episode stills are not regenerated when only the DB URL field changes from fanart to still. Logo overlay stamp uses a **content hash** of the SVG (not file mtime) so container restarts do not force a full poster rewrite.
- **Top banner overlay typography**: Bundles **Space Grotesk Bold** (same family as onboarding `font-headline`) in the image so top-banner “PLACEHOLDER” text renders at the requested size instead of Pillow’s tiny default bitmap font on slim Python images. Banner proportions are slightly larger for legibility in Plex grids; changing the font or layout bumps the overlay stamp so art refresh can regenerate posters.
- **Art bulk completion order**: Last art batch marks the full-sync task **DONE** before firing Plex/Jellyfin/Emby section refresh HTTP (refresh is fire-and-forget; we do not wait for Plex library scans to finish).
- **ARR Integrations — shared placeholder cleanup**: Split the shared-folder cleanup setting into separate Radarr and Sonarr controls (two-column layout, first under behavior options). Legacy `MULTI_INSTANCE_SHARED_PLACEHOLDER_CLEANUP` values still apply when the new keys are unset.
- **Shared placeholder cleanup (aggressive mode)**: “Remove when any instance has a real file” now applies across sync and materialization, not only on-disk delete: when a sibling instance has `has_file` for the same TMDB id or TVDB+season+episode, other instances are marked `not_needed` and stale placeholders are cleaned up instead of recreated on the next sync.
- **Task schedule persistence**: Next run times for full/lite sync are stored in AppConfig and survive restarts; manual and scheduled completions reset the interval from completion time (no boot-time stagger).
- **Task queue progress**: Task history rows expand to show phased progress sections (nested `progress.progress.sections` supported). Scheduled-task cards show elapsed time while a run is still working.

### Fixed

- **Playback search suppression toggles**: “Do not search already-monitored episodes” only applies when that setting is enabled (default off searches the full target set again). Lookahead settings are reordered (Monitor only first); the two filters grey out while monitor-only is on and restore their prior on/off state when monitor-only is turned off.
- **Startup task abandon**: Orphaned working runs were not cleared on restart because `Job` was missing from `task_run_history` imports (`name 'Job' is not defined`).
- **Episode art skip**: Episode stills in the same season folder shared one `episode_thumb` entry in `.poster-overlay.json`, so each run rewrote ~10k thumbs; meta keys are now per file (`episode_thumb:<basename>-thumb.jpg`).
- **Full sync stuck WORKING after art finished**: Art completion now calls `finalize_art_backfill_phase` (was missing in the art reconciler’s “run complete” path). Follow-up job checks count only **PENDING** / **CLAIMED** jobs, not **WORKING**, so the last art batch no longer blocks itself when closing the task. `accumulate_art_backfill_counts` and `reconcile_stuck_art_backfill_tasks` (on Tasks API load) repair runs where all batches finished but the parent row stayed open, including when the art phase is already **Done** but the task is still **Working**.
- **Corner badge overlay**: Uses the real Placeholdarr mark exported from `Placeholdarr_yellow.svg` (replacing the temporary block-letter asset) and places the badge in the **bottom-right** corner (avoids Plex’s unwatched-episode count on TV posters). Poster regeneration runs automatically when the logo asset or layout stamp changes.
- **Placeholder refresh intent**: Template-only future saves no longer clear unrelated pending metadata work; partial Apply-now enqueues clear committed domains correctly; full intent clear also resets legacy template/art pending flags on upgraded installs.
- **Library poster refresh in UI**: Library polling digest includes `poster_url` so poster token updates apply without a full page reload; stale `poster-grid.jpg` files are ignored when overlay mode is off.

## [0.9.11] - 2026-05-18

### Added

- **Status message live preview API**: `POST /api/messages/preview` returns dual movie/episode sample lines (title + synopsis), honors draft projection mode and status-update scope, and reports empty tokens for the REQUEST synopsis template.
- **Unified projection context for NFOs and players**: Media tokens (movie/episode/season/series) plus runtime flow through the same helper so REQUEST lines and title suffixes match between sidecar NFOs and direct player projection.
- **Template apply-now coalescing**: Repeated “Apply now” supersedes pending template `nfo_refresh` jobs and tracks an active run id so only the latest sweep triggers the one-shot Plex library refresh when the final batch finishes.
- **DB pool checkout telemetry**: Optional instrumentation (`services/postgres/pool_telemetry.py`) registers on the SQLAlchemy engine to log near-exhaustion snapshots (active thread + hold time), warn on slow check-ins, and set Postgres `application_name` to `ph_*` on checkout for correlation with `pg_stat_activity`. Tunable via `DB_POOL_TELEMETRY_ENABLED`, `DB_POOL_SLOW_CHECKIN_LOG_SECONDS`, `DB_POOL_NEAR_FULL_LOG_COOLDOWN_SECONDS`, and `DB_POOL_NEAR_FULL_FREE_SLOTS`.

### Changed

- **Settings → Status Updates** now includes the customizable status message templates (the old **Status Messages** tab is removed; `/settings/status-messages` redirects to Status Updates). Save blocks when templates have validation errors; saving persists field-backed settings first, then templates, so workers do not mix stale projection mode with new template text.
- **Message registry (labels)**: Per-stage `status.label.*` and legacy bracket keys are dropped from the user-facing list; REQUEST uses **Request line (synopsis)** with expanded tokens (including TV fields). Title projection uses separate **title suffix** strings per surface (`movie`, `series`, `season`, `episode`) instead of `{Title}` / `{Bracket}` formatting.
- **Status projection**: Projection mode maps to title and/or summary surfaces explicitly; stripping removes both square and angle bracket prefixes/suffixes where applicable.
- **Docker entrypoint**: Creates `passwd`/`group` entries for `PUID`/`PGID` when missing so `setpriv` and `chown` work on slim base images.

### Fixed

- **Worker NFO backfill completion query**: Job payload run-id filters use SQLAlchemy JSON `.as_string()` (generic `JSON` columns), fixing `astext` crashes when completing coordinated backfill batches.
- **Dashboard stats snapshot refresh**: Refreshes that recompute `/api/stats` materialized counters now take a Postgres session advisory lock (`pg_try_advisory_lock`) so only one `UPDATE dashboard_stats_snapshot` runs at a time—concurrent hooks skip instead of stacking many sessions on the singleton row. The refresh transaction also uses `SET LOCAL lock_timeout = '15s'` so a stray blocker fails fast instead of wedging the pool for hours (mitigates `idle in transaction` + `transactionid` / `tuple` wait chains that could surface as repeated worker claim `lock_timeout` warnings).
- **Queue monitor NOTIFY wake race on idle sleep**: The producer no longer calls `_wake_event.clear()` before waiting when the idle probe finds no active placeholders. A playback-triggered NOTIFY between that probe and `clear()` could be erased and delay SEARCHING/DOWNLOADING updates until the safety poll (default 300s); the idle path now waits only when the event is unset and clears after wake (aligned with the worker drain-race fix).

### Summary

Placeholdarr used to wake background workers on a fixed timer and ask the database “is there work?” over and over. That was simple and predictable, but under load it meant many threads hitting Postgres even when nothing had changed, and work could sit in the queue until the next poll interval.

The app now uses Postgres **LISTEN / NOTIFY**: when a job is created or becomes runnable, the database signals the workers immediately so they **react as soon as something happens**—webhooks, refreshes, and queue activity feel snappier instead of waiting on a polling cadence. You should see **less idle database chatter** (fewer round-trips when the system is quiet) and **better responsiveness when many things happen at once**, because workers are napped until there is actually work.

Alongside that switch, this release tightens **how long the app holds database connections** during slow steps (Plex, Radarr/Sonarr, disk). That reduces the “everyone waiting on everyone else” stalls and connection-pool exhaustion that showed up once NOTIFY made work arrive in bursts instead of dribbling in over time. Net effect: similar or lower steady-state DB load when idle, and **smoother behavior under spikes**—assuming Postgres is sized normally for your library.

Operators: job NOTIFY uses modern Postgres trigger syntax; **Postgres 11+** is recommended (older servers fall back automatically where possible). Details below for tuning and diagnostics.

### Job queue, NOTIFY, and workers

- **Claim / finish model:** claim uses its own session and returns a detached `ClaimedJobDescriptor`; handlers no longer mark jobs DONE. Finish uses `_mark_descriptor_done` / `_mark_descriptor_failed` with conditional updates on `claim_token` (`StaleJobClaimError` on races). `WORKER_CLAIM_LOCK_TIMEOUT_SECONDS` and clearer stall heartbeats for lock contention on claim and finish.
- **Reaper and wakeups:** `claim_token` on claims; reaper clears token and wakeups via `RETURNING` + `_drain_event.set()`. `WORKER_MAX_JOBS_PER_DRAIN` limits burst work per pass.
- **NOTIFY behavior:** `_drain_event.clear()` before each drain. `WORKER_NOTIFY_ENABLED` and `WORKER_FALLBACK_POLL_SECONDS` to bypass LISTEN; `WORKER_SAFETY_POLL_SECONDS` (15) and `WORKER_STALE_CLAIMED_RESET_SECONDS` (900) as tighter defaults. Notifier `healthy()` / `get_shared_notifier_health()` for ops.
- **Idempotency and priority:** `Job.priority` with `job_priority` defaults; `processed_job_key` and helpers for NFO / media side effects; `mark_claim_revoked` / `is_claim_revoked` for long handlers. `class_singleton` gate on `run_calendar_phase`.
- **LISTEN/NOTIFY stack:** dedicated `Notifier` with DB triggers on `job`; worker threads + safety poll + stale-CLAIMED reaper. `DisplayStatus.SEARCH_QUEUED` during startup gate. Durable `media_refresh` and `startup_sync_runner` jobs (env to fall back to Timers / threads). Queue monitor uses `queue_monitor_active`, NOTIFY `placeholdarr_queue_monitor_signal`, and safety poll. Legacy interval-polling worker and `USE_NOTIFY_*` toggles removed. Lifespan startup-gate watchdog for wedged sync.
- **Logging:** per-job `job_done … elapsed_s=…` and optional `queue_wait_s=…` when `enqueued_at` is present; removed redundant `job_slow` line.

### Database, pool, and schema

- **Pool env:** `DB_POOL_SIZE`, `DB_POOL_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, `DB_POOL_RECYCLE_SECONDS` (replaces hardcoded pool in `db.py`). `session_scope()`; fix for `StatusOrchestrator._get_session` session leak.
- **Pool telemetry env:** `DB_POOL_TELEMETRY_ENABLED` (default on), `DB_POOL_SLOW_CHECKIN_LOG_SECONDS`, `DB_POOL_NEAR_FULL_LOG_COOLDOWN_SECONDS`, `DB_POOL_NEAR_FULL_FREE_SLOTS`; hooks registered from `get_engine()` when enabled.
- **Dashboard stats snapshot:** single-flight advisory lock + bounded `lock_timeout` on the post-commit refresh path (`dashboard_stats_snapshot_hooks`) to prevent many pooled connections blocking on one row.
- **Ops:** `GET /api/diagnostics/db` (pool, `pg_stat_activity` / blockers, long xacts, notifier snapshot).
- **Startup migrations:** blocking `pg_advisory_lock` with `lock_timeout` and `RUNTIME_SCHEMA_LOCK_WAIT_SECONDS` (replaces short `pg_try` retries and spurious warnings); unlock in `finally`. Engine log line uses real pool numbers (f-string). Job NOTIFY triggers: `EXECUTE FUNCTION` vs `EXECUTE PROCEDURE` by server version.

### Handlers, sync, and long I/O

- **Session boundaries:** `media_refresh` and NFO / `refresh_all_sections` paths release the DB before long HTTP; `queue_monitor_producer._poll_once` is scan → ARR HTTP → apply. `startup_sync_runner` uses a row `app_config` gate and heartbeat (not a long-held session advisory lock).

### Calendar and status

- **Scope:** `run_calendar_phase` uses a release-date window (with TBA / coming-soon handling) instead of scanning all on-disk placeholders; **`CALENDAR_PHASE_BATCH_SIZE`** commits per batch with per-chunk logging and `chunks_committed` in stats.

### Reliability fixes

- **Plex metadata:** skip heavy `find_movie_by_id` when the expected placeholder file is missing on disk.

## [0.9.10] - 2026-05-05

### Changed

- **Radarr/Sonarr webhooks**: `Grab` is recognized as informational (`movie_grab` / `episode_grab`) and skipped without warnings; `movie_imported` uses the same ARR instance resolution as `movie_added` and can **self-heal** by upserting from the Radarr API when the DB row is missing (e.g. after a failed `MovieAdded` job).
- **Library "Future" matches calendar lookahead (not plain `not_needed`)**
  - Movies/TV library filters and `/api/stats` "future outside lookahead" use the same air/release date vs `CALENDAR_LOOKAHEAD_DAYS` rules as `_compute_determination`. Policy-only `not_needed` rows (e.g. season 0 specials when `INCLUDE_SPECIALS` is false) no longer count as Future.
- **REQUEST NFO backfill now runs in bulk NFO-only mode**
  - Startup REQUEST backfill enqueues `nfo_refresh` jobs with direct player projection disabled so large libraries are not bottlenecked by per-item Plex/Jellyfin/Emby metadata writes during catch-up.
- **Backfill completion now triggers one library refresh**
  - Backfill jobs are tagged with a run id; when the last job in that run completes, the app triggers a single section refresh (`movies + episodes`) so players pick up updated NFO text in one pass.
- **Backfill queue isolation**
  - REQUEST backfill enqueues with pending-job merge disabled to avoid inheriting older mixed payloads and to keep backfill behavior deterministic.
- **Persisted projected display status in DB**
  - Added `placeholder.display_status_projected` so the user-facing status text is stored persistently (including REQUEST runtime bracket text like `[1h 43m · REQUEST]`) whenever status is written by orchestrator/materializer/import-grace paths.
- **Plex force metadata refresh for REQUEST backfill completion**
  - The one-time REQUEST NFO backfill completion refresh now calls Plex section refresh with `force=1` so existing library items are re-read for metadata changes from NFOs; normal status update paths continue to use direct projection behavior.
- **Docker**: example compose sets `PUID`/`PGID` and bind mounts for appdata plus a placeholder/media root; startup entrypoint `chown`s `/config` and `/app`, then runs the app non-root via `setpriv`.

### Documentation

- **README**: Plex playback automation typically uses **Tautulli** (or similar) with Placeholdarr’s webhook URL.

### Fixed

- **`movie_added` webhook crash on first-time movie ingest (`int(None)`)**
  - New `Movie` rows from `_upsert_movie` had no database primary key until a later `flush`, but `process_movie_add_event` called `_sync_linked_placeholder_presence(..., movie_id=int(movie_row.id), ...)` first — so brand-new titles (no prior DB row) raised `TypeError` and left `movie_imported` follow-ups without a catalog row.
  - `_upsert_movie` and `_upsert_episode` now `flush()` immediately after inserting a new row (aligned with `_upsert_series` / `_upsert_season`) so `.id` is valid before any code reads it.

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
- **Detail ARR deep links**: Movie and series detail APIs include `**arr_instance_links`** (label + URL per Radarr/Sonarr instance that holds the same TMDB/TVDB title). `**arr_instance_links`** now also carries `**has_file` / `has_placeholder`** per movie row and `**episode_files` / `episode_placeholders**` per series row (Sonarr episode aggregates). The dashboard shows a bottom **launch row** with the service logo and configured instance name; the calendar spotlight can open multiple instance links when present.
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

