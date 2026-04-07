# Placeholdarr

Placeholdarr is an AI-developed application conducted by TheIndieArmy and built from the ground up, inspired by [Infinite Plex Library](https://github.com/arjanterheegde/infiniteplexlibrary) and [Chronicle](https://github.com/iwouldratherbeatthebeach/chronicle/tree/main). 

## Credits

- **Jellyfin support integration:** Thanks to [Priky-one](https://github.com/Priky-one) for implementing Jellyfin support.
- **GHCR Docker workflow support:** Thanks to [aves-omni](https://github.com/aves-omni) for GitHub Container Registry workflow integration.

## Overview

Placeholdarr bridges the gap between media discovery and storage management. It allows you to maintain a comprehensive **Plex or Jellyfin** library without the storage overhead of keeping everything downloaded at once.

### Automated Library Building

Leverage Radarr/Sonarr's import lists to their full potential:
- Add entire collections, lists, or genres
- Create placeholders for everything automatically
- Browse massive libraries in Plex immediately
- Only download what users actually want to watch
- Perfect for large collections like IMDb Top 250, studios, or director filmographies

### How It Works

1. Add content to Radarr/Sonarr unmonitored and without starting a search
2. Placeholdarr creates lightweight placeholder files in your Plex libraries
3. Users see titles available in Plex, just as if they were downloaded
4. When someone plays a placeholder:
   - The real content is automatically searched for in arrs
   - Plex summary updates show download/request status (see below)
   - Placeholder is replaced with actual media when ready

### Integration Benefits

- **Storage Efficiency**: 
   - Add automated lists without the storage committment
   - Reduce user requests taking up storage for things that won't be watched for weeks later, months later, or even never
   - Combine with Maintainerr for automated retention without your Plex users losing sight of their desired content
- **Full Library Visibility**: Users can see everything in arrs, not just downloaded content
   - Don't want everything in arrs showing in Plex? Utilize tags in arrs to control what content gets placeholders made and shown in Plex
- **Automation Ready**: Works with other tools in your stack:
   - *Radarr/Sonarr* for downloads and library management
   - *Plex* and *Jellyfin* for streaming
   - *Overseerr* for requests (Optional)
     - Simply disable automatic search for requests
     - A placeholder will be made when the request gets added to arrs
     - User sees the title as a placeholder in Plex or Jellyfin and triggers the search when they play it
     - Saves you the storage space until the user is actually ready to watch
   - *Maintainerr* for storage management - Can be set up so when a real file is deleted, a placeholder is created to keep it visible in Plex or Jellyfin (Optional)
     - Be sure to turn on the "On File Delete" trigger in your arrs webhook settings
     - Replaces content not being watched with a placeholder
     - Keeps content visible to users in Plex or Jellyfin to re-download when they are ready to watch

Perfect for:
- Large libraries with limited storage
- Media servers with multiple users
- Automated media management setups
- Collections that exceed available storage

---

## Key Features

- **Jellyfin/Emby Support:**  
  Placeholdarr now works with Jellyfin/Emby as well as Plex. All placeholder, status, and automation features are available for both servers.
- **Automatic Placeholder Creation:**  
  Creates dummy video files for missing movies and TV episodes, so users can see and request unavailable content in Plex, Jellyfin, or Emby.
- **Calendar-Based Status Sync:**  
  Periodically syncs with Sonarr/Radarr calendars to create placeholders and update statuses for upcoming content (e.g., "Coming Soon", "Request").
- **Status in Summary/Description:**  
  Statuses (e.g., "Coming Soon", "Request", "Searching...") are now prepended to the summary/description field for both movies and TV episodes, ensuring visibility in all Plex and Jellyfin clients (including mobile).
- **Queue Monitoring:**  
  Tracks download/search progress and updates status in Plex or Jellyfin as content moves through the queue.
- **Highly Configurable:**  
  Supports lookahead windows, "Coming Soon" toggles, preferred movie date types, and more.


---

## Configuration

### Appdata Layout

For Docker deployments, use a single Appdata root for Placeholdarr state and logs.

Recommended host layout:
- `/mnt/user/appdata/placeholdarr` for Placeholdarr app data and logs
- `/mnt/user/appdata/placeholdarr/postgres` for Postgres data

Container layout with the repo default compose file:
- `/config` for Placeholdarr app data
- `/config/logs/placeholdarr-*.log` for per-run application logs (timestamped files, e.g., `placeholdarr-2026-03-31-143052.log`)
- `/var/lib/postgresql/data` backed by `${PLACEHOLDARR_APPDATA}/postgres`

The included [docker-compose.yml](docker-compose.yml) now defaults to:
- `${PLACEHOLDARR_APPDATA:-./.appdata}:/config`
- `${PLACEHOLDARR_APPDATA:-./.appdata}/postgres:/var/lib/postgresql/data`

The included [docker-compose.override.yml](docker-compose.override.yml) is now intentionally optional and local-only (for machine-specific port/network/path tweaks).

This keeps logs, runtime state, and database storage under one Appdata root while leaving your media libraries mounted separately. Logs are created per app run and old runs are automatically cleaned up to stay under the configured limit.

### Library Folder Strategies

To use Placeholdarr, create a dedicated folder for placeholders (for example, `/mnt/user/data/placeholder-movies` and `/mnt/user/data/placeholder-tv`).

Once you have created your placeholder folders, you have two options for adding them to your Plex and/or Jellyfin libraries:

#### Option 1: Add Placeholders and Real Files in the Same Library
- Add both your placeholder folder(s) and your real file folder(s) to the same library in Plex/Jellyfin.
- Both real files and placeholders will appear together in the same library. This is simpler to set up, but does not provide as much clarity or separation between real and placeholder content.

#### Option 2: Add Placeholders and Real Files as Separate Libraries
- Add your placeholder folder(s) as a separate library in Plex/Jellyfin (for example, a "Requests" library).
- Keep your real file libraries (e.g., `/mnt/user/data/movies`, `/mnt/user/data/tv`) as separate libraries.
- This setup makes it clear to users which items are placeholders (requests) and which are available to play immediately, and keeps your real and placeholder files fully separated for easy management and cleanup.

**Note:**  
Placeholdarr does not need to know your *arr root folders—just set the library folders to wherever you want placeholders to appear.

---

### Environment Variables

Onboarding-managed defaults:
- Placeholdarr now starts with integration/path settings optional so first-run onboarding can be completed in the UI.
- You can still pre-seed values in `.env`, but most operators should leave onboarding-managed settings commented and configure them in the dashboard.

Recommended minimum settings to run placeholders end-to-end:
- At least one media server enabled (`PLEX_*`, `JELLYFIN_*`, or `EMBY_*`)
- At least one ARR pair (`RADARR_*` and/or `SONARR_*`) depending on your media goals
- Placeholder output path strategy (`LIBRARY_ROOT` and/or explicit movie/tv folders)
- `DUMMY_FILE_PATH` for standard placeholder media

Path behavior summary:
- `LIBRARY_ROOT` is optional and can be used as a shared base.
- `MOVIE_LIBRARY_FOLDER` and `TV_LIBRARY_FOLDER` are optional explicit overrides.
- 4K folder fields are optional and only needed when you want separate 4K placeholder locations.

Optional settings:
- `APPDATA_PATH`: In-container app data root used for defaults such as logs (default `/config`)
- `LOG_DIR`: Directory for per-run log files (default `${APPDATA_PATH}/logs`)
- `LOG_FILE`: Full path to log directory (overrides `LOG_DIR` when set)
- `LOG_MAX_RUN_FILES`: Maximum number of per-run log files to keep before deleting oldest ones (default `10`)
- `PLACEHOLDER_STRATEGY`: How to create placeholders (`hardlink` or `copy`)
- `PLACEHOLDER_CREATE_NFO`: Create `.nfo` sidecar metadata next to placeholder files (`true`/`false`, default `true`)
- `PLACEHOLDER_STATUS_UPDATES`: Controls whether Placeholdarr writes placeholder status text updates (`OFF`, `REQUEST`, `ALL`)
- `PLACEHOLDER_STATUS_PROJECTION_MODE`: Controls where status text is projected when status updates are enabled: `summary`, `title`, `both`, `off`
  - If `PLACEHOLDER_STATUS_UPDATES=OFF`, no status text is written, so projection output will not be shown regardless of projection mode.
- `PLACEHOLDER_FILE_MODE`: Octal file mode applied to placeholders and `.nfo` files (default `666`)
- `PLACEHOLDER_DIR_MODE`: Octal directory mode applied to placeholder folders (default `777`)
- `TV_PLAY_MODE`: Download scope (`episode`, `season`, or `series`)
- 4K support settings (if needed)
- `RADARR_STD_INSTANCE_KEY`, `RADARR_4K_INSTANCE_KEY`, `SONARR_STD_INSTANCE_KEY`, `SONARR_4K_INSTANCE_KEY`: Optional webhook/sync instance key overrides (defaults: `radarr_std`, `radarr_4k`, `sonarr_std`, `sonarr_4k`)
- `ARR_INSTANCES_JSON`: Optional advanced JSON array for many named ARR instances (overrides fixed std/4K URL/API-key pairs when set)
  - Example:
    - `[ {"arr_type":"radarr","instance_key":"radarr_main","label":"Main Radarr","url":"http://radarr:7878/api/v3","api_key":"...","is_4k":false}, {"arr_type":"sonarr","instance_key":"sonarr_anime","label":"Anime Sonarr","url":"http://sonarr-anime:8989/api/v3","api_key":"...","is_4k":false} ]`
- `TAUTULLI_INSTANCE_KEY`, `JELLYFIN_INSTANCE_KEY`, `EMBY_INSTANCE_KEY`: Optional playback webhook source key overrides (defaults: `tautulli`, `jellyfin`, `emby`)
- **Playback event handler settings** (requires a Tautulli/Jellyfin/Emby webhook; disabled by default):
  - `ENABLE_PLAYBACK_EVENT_HANDLERS`: Activate playback-driven ARR searches (`true`/`false`, default `false`)
  - `PLAYBACK_SEARCH_PREFERENCE`: Which ARR instance to search when a placeholder is played (`standard`, `4k`, `both`, default `both`)
  - `TV_PLAYBACK_INSTANCE_MODE`: How to select the Sonarr instance for TV episodes (`match`, `preference`, `both`, default `match`)
    - `match` — use the instance whose library path matches the played file path
    - `preference` — always follow `PLAYBACK_SEARCH_PREFERENCE`
    - `both` — always trigger both standard and 4K Sonarr instances
  - `PLAYBACK_FALLBACK_TIMEOUT_MINUTES`: Minutes before a delayed fallback ARR search fires if the preferred instance has not yet imported the title (default `30`; `0` disables fallback)
  - `PLAYBACK_COOLDOWN`: Seconds to suppress duplicate playback events for the same item (default `30`; `0` disables)
- `INCLUDE_SPECIALS`: Include specials in TV placeholder creation (`true`/`false`)
- `EPISODES_LOOKAHEAD`: Number of episodes to look ahead and download (integer)
- `MAX_MONITOR_TIME`: Maximum time to monitor for file in seconds (integer)
- `CHECK_INTERVAL`: How often to check queue status in seconds (integer)
- `AVAILABLE_CLEANUP_DELAY`: Delay before removing monitored item after it becomes available (integer)
- `FULL_SYNC_INTERVAL_HOURS`: Source-of-truth full sync cadence in hours for Radarr/Sonarr recurring runs (`0` disables recurring runs; minimum effective interval is `1` hour)
- **Calendar-based status update settings:**
  - `CALENDAR_LOOKAHEAD_DAYS`: How many days into the future to allow placeholders/"Coming Soon" (integer)
    - `> 0`: strict horizon in days for the selected release type.
    - `0`: disables future placeholder lookahead behavior (future placeholders are reconciled out).
    - `< 0` (for example `-1`): infinite lookahead.
  - `CALENDAR_SYNC_INTERVAL_HOURS`: Independent calendar/date-refresh scheduler cadence in hours (`0` disables independent calendar scheduler)
    - This scheduler runs lightweight date refresh + determination/materialization/calendar/status reconcile without a full ARR sync.
  - `PREFERRED_MOVIE_DATE_TYPE`: Which movie release date to use (`inCinemas`, `digitalRelease`, `physicalRelease`)
    - This is the selected movie date type for status text and lookahead decisions.
    - Strict behavior: Placeholdarr does not fallback to another release type when this date is missing.
    - TBA status text is shown only when lookahead is infinite (`CALENDAR_LOOKAHEAD_DAYS < 0`) and the selected release date is unavailable.
    - Movie status text uses release type wording when possible, for example: `Digital release in 12 days`, `Theatrical release today`, `Physical release was 5 days ago`.
  - `ENABLE_COMING_SOON_COUNTDOWN`: Show countdown in "Coming Soon" status (`true`/`false`)
  - `CALENDAR_PLACEHOLDER_MODE`: Add placeholders as each episode enters lookahead window (`episode`) or add all known episodes of a season when any enters window (`season`)

Calendar freshness notes:
- Full sync refreshes broad content metadata and date fields.
- Independent calendar scheduler refreshes only date-relevant metadata via ARR calendar-range calls.
- Visibility rules still strictly follow `CALENDAR_LOOKAHEAD_DAYS` and selected `PREFERRED_MOVIE_DATE_TYPE`; future-buffer fetch does not expand what users see.

---

### Placeholder Video Files

- `DUMMY_FILE_PATH`: Path to your standard dummy video file (used for available/requestable placeholders).
- `COMING_SOON_DUMMY_FILE_PATH`: (Optional) Path to a special dummy video file used for "Coming Soon" placeholders (future releases).  
  If not set, the standard dummy file will be used for all placeholders.
- In onboarding, blank dummy paths are pre-filled from `APPDATA_PATH` defaults (`${APPDATA_PATH}/dummy.mp4` and `${APPDATA_PATH}/coming_soon_dummy.mp4`).
- Placeholdarr now writes a `.nfo` sidecar beside each placeholder by default to support faster Jellyfin/Plex metadata refresh workflows.

---

### Disabling Plex or Jellyfin

- **To disable Plex:**  
  Leave `PLEX_URL`, `PLEX_TOKEN`, `PLEX_MOVIE_SECTION_ID`, and `PLEX_TV_SECTION_ID` blank in your `.env` file.

- **To disable Jellyfin:**  
  Leave `JELLYFIN_URL` and `JELLYFIN_TOKEN` blank in your `.env` file.

**Note:**  
For a useful production setup, configure at least one media server integration (Plex, Jellyfin, or Emby). Placeholdarr auto-detects enabled servers from configured values.

---

### Webhook Setup Overview

Placeholdarr receives all webhook traffic on `/webhook` and routes each request by the required `instance` query parameter.

Use this URL format for every sender:
- `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<value>`

Allowed `instance` values are configurable via ENV instance-key settings.

Default values:
- `radarr_std` (`RADARR_STD_INSTANCE_KEY`)
- `radarr_4k` (`RADARR_4K_INSTANCE_KEY`)
- `sonarr_std` (`SONARR_STD_INSTANCE_KEY`)
- `sonarr_4k` (`SONARR_4K_INSTANCE_KEY`)
- `tautulli` (`TAUTULLI_INSTANCE_KEY`)
- `jellyfin` (`JELLYFIN_INSTANCE_KEY`)
- `emby` (`EMBY_INSTANCE_KEY`)

Current behavior:
- Requests without a valid configured `instance` value are rejected with HTTP 400.
- Placeholdarr does not infer sender identity from payload content.
- The only deployment-specific parts are your server address and `PLACEHOLDARR_PORT`.

---

### Radarr Webhook Setup

- For more-tailored control of content, utilize tags to determine what titles get placeholders created for them.

1. In Radarr, go to Settings → Connect → Add Connection (Plus Icon)
2. Select "Webhook"
3. Configure:
   - Name: PlaceholdARR
  - Standard Radarr URL: `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<RADARR_STD_INSTANCE_KEY>`
  - 4K Radarr URL: `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<RADARR_4K_INSTANCE_KEY>`
   - Method: POST
   - Triggers (enable only):
     - On Grab
     - On File Import
     - On Movie Added
     - On Movie Delete
     - On Movie File Delete

---

### Sonarr Webhook Setup

1. In Sonarr, go to Settings → Connect → Add Connection (Plus Icon)
2. Select "Webhook"
3. Configure:
   - Name: PlaceholdARR
  - Standard Sonarr URL: `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<SONARR_STD_INSTANCE_KEY>`
  - 4K Sonarr URL: `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<SONARR_4K_INSTANCE_KEY>`
   - Method: POST
   - Triggers (enable only):
     - On Grab
     - On File Import
     - On Series Add
     - On Series Delete
     - On Episode File Delete

---

### Tautulli Webhook Setup

Required Tautulli webhook URL pattern:
- `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<TAUTULLI_INSTANCE_KEY>`

1. In Tautulli, go to Settings → Notification Agents
2. Add a new Webhook notification agent
3. Configure the webhook:
  - Webhook URL: `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<TAUTULLI_INSTANCE_KEY>`
   - Trigger: Playback Start
   - Payload Format: JSON
4. Use this JSON payload:
```json
{
    "event": "playback.start",
    "media": {
        "type": "{media_type}",
        "title": "{title}",
        "show_name": "{show_name}",
        "episode_name": "{episode_name}",
        "season_num": "{season_num}",
        "episode_num": "{episode_num}",
        "year": "{year}",
        "ids": {
            "plex": "{rating_key}",
            "tmdb": "{themoviedb_id}",
            "tvdb": "{thetvdb_id}",
            "imdb": "{imdb_id}"
        },
        "file_info": {
            "path": "{file}"
        }
    }
}
```

---

### Jellyfin Webhook Setup

Required Jellyfin webhook URL pattern:
- `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<JELLYFIN_INSTANCE_KEY>`

1. In Jellyfin, go to **Dashboard → Plugins → Catalog** and install the **Webhook** plugin if not already installed.
2. Go to **Dashboard → Plugins → Webhook** and click **Add Webhook**.
3. Set the **Webhook URL** to:
   ```
  http://your-server:PLACEHOLDARR_PORT/webhook?instance=<JELLYFIN_INSTANCE_KEY>
   ```
   Replace `your-server` and `PLACEHOLDARR_PORT` with your actual address and configured Placeholdarr port.
4. Under **Events**, enable **Playback Start**.
5. Set **Content Type** to `application/json`.
6. Use this as the **Payload Template**:
   ```json
   {
     "event": "playback.start",
     "ItemId": "{{ItemId}}",
     "UserId": "{{UserId}}",
     "Name": "{{Name}}",
     "ItemType": "{{ItemType}}",
     "SeriesName": "{{SeriesName}}",
     "SeasonNumber": "{{SeasonNumber}}",
     "EpisodeNumber": "{{EpisodeNumber}}",
     "Provider_tmdb": "{{Provider_tmdb}}",
     "Provider_tvdb": "{{Provider_tvdb}}",
     "Provider_imdb": "{{Provider_imdb}}",
     "Year": "{{Year}}",
     "NotificationType": "{{NotificationType}}"
   }
   ```
7. Save the webhook.

---

### Emby Webhook Setup

Required Emby webhook URL pattern:
- `http://your-server:PLACEHOLDARR_PORT/webhook?instance=<EMBY_INSTANCE_KEY>`

1. In Emby, go to **Settings → Notifications**.
2. Add or edit your webhook notification.
3. Set the **Webhook URL** to:
   ```
  http://your-server:PLACEHOLDARR_PORT/webhook?instance=<EMBY_INSTANCE_KEY>
   ```
   Replace `your-server` and `PLACEHOLDARR_PORT` with your actual address and configured Placeholdarr port.
4. Enable the playback-start event you want Placeholdarr to receive.
5. Save the webhook.

---

### Playback Behavior Settings

Placeholdarr's playback event handlers are opt-in.  After configuring one or more of the webhook sources above, enable the feature and tune how searches are triggered:

| Setting | Default | Description |
|---|---|---|
| `ENABLE_PLAYBACK_EVENT_HANDLERS` | `false` | Set to `true` to activate playback-driven ARR searches |
| `PLAYBACK_SEARCH_PREFERENCE` | `both` | `standard`, `4k`, or `both` — which ARR instance to search for movies |
| `TV_PLAYBACK_INSTANCE_MODE` | `match` | `match` (path-based), `preference` (follow above), or `both` |
| `PLAYBACK_FALLBACK_TIMEOUT_MINUTES` | `30` | Minutes before fallback fires if preferred instance has not imported; `0` disables |
| `PLAYBACK_COOLDOWN` | `30` | Seconds to deduplicate repeated playback events for the same item; `0` disables |

**How it works:**
- **Placeholder played:** Placeholdarr immediately triggers an ARR search on the selected instance(s) according to `PLAYBACK_SEARCH_PREFERENCE` / `TV_PLAYBACK_INSTANCE_MODE`.
- **Real file played:** If the other (standard vs. 4K) instance does not yet have the file, a search can optionally be triggered there too.
- **Fallback:** If the preferred instance does not import within `PLAYBACK_FALLBACK_TIMEOUT_MINUTES`, a fallback job searches the alternate instance.
- **Deleted/ignored titles:** Content marked `is_deleted=True` in Placeholdarr is always skipped.

---

## Docker Usage Notes

If you are running Placeholdarr in Docker, you **must provide a `dummy.mp4` file** on the host and mount it into the container.  
You can use the sample `dummy.mp4` provided in this repository, or supply your own small/valid video file if you prefer.

**Instructions:**
1. **Download the sample dummy file:**  
   - Download `dummy.mp4` from the Placeholdarr GitHub repository and save it to your host (e.g., `/path/to/dummy.mp4`).
2. **Or use your own:**  
   - You may use any small/valid video file as a placeholder.
3. **Mount it into the container:**  
   - In your `docker-compose.yml` or `docker run` command, mount it to `/data/dummy.mp4` inside the container:
     ```yaml
     volumes:
       - /path/to/dummy.mp4:/data/dummy.mp4
     ```
4. **Set the path in your `.env`:**  
   - `DUMMY_FILE_PATH=/data/dummy.mp4`

---

## What's New

- **Jellyfin support:** All placeholder, status, and automation features now work with Jellyfin.
- **Unified status updates:** Statuses are now shown in the summary/description field for both Plex and Jellyfin, ensuring visibility in all clients.
- **Automatic webhook detection:** Placeholdarr automatically distinguishes between Tautulli (Plex) and Jellyfin webhook payloads.
- **Batch calendar sync:** Improved efficiency and reliability for placeholder creation and status updates.
- **Improved configuration:** More `.env` options for calendar, queue, and placeholder management.

---

## Additional Features

- 4K Support: Configure separate 4K instances of Radarr/Sonarr
- TV Play Modes: Choose between episode/season/series downloads
- Hardlink/Copy: Choose how placeholder files are created
- Progress Tracking: Monitor downloads in Plex titles (Development in-progress, recommend using the Off or Request setting in ENV for now.)
- Auto Cleanup: Removes placeholders when downloads complete

## Troubleshooting

Common issues:
1. Port in use: Service cleans port 8000 on startup
2. Missing dummy.mp4: Create an empty file or small video
3. Webhook not triggering: Check Tautulli condition/payload
4. Download not starting: Verify *arr API keys and URLs
