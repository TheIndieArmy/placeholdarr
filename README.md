# Placeholdarr

**Placeholdarr** is an AI-developed application that bridges the gap between media discovery automation and storage management. Conducted by TheIndieArmy and built from the ground up, inspired by [Infinite Plex Library](https://github.com/arjanterheegde/infiniteplexlibrary) and [Chronicle](https://github.com/iwouldratherbeatthebeach/chronicle). 

## Credits

- **Jellyfin support integration:** Thanks to [Priky-one](https://github.com/Priky-one) for implementing Jellyfin support.
- **GHCR Docker workflow support:** Thanks to [aves-omni](https://github.com/aves-omni) for GitHub Container Registry workflow integration.

## Overview

Placeholdarr allows you to maintain a comprehensive **Plex, Jellyfin, or Emby** library without the storage overhead of keeping everything downloaded at once. It automates placeholder creation, status tracking, and on-demand downloads.

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
   - Plex summary updates show download/request status
   - Placeholder is replaced with actual media when ready

### Integration Benefits

- **Storage Efficiency**: 
   - Add automated lists without the storage commitment
   - Reduce user requests taking up storage for things that won't be watched for weeks later, months later, or even never
   - Combine with Maintainerr for automated retention without your Plex users losing sight of their desired content
- **Full Library Visibility**: Users can see everything in *arrs, not just downloaded content
   - Don't want everything in *arrs showing in Plex/Jellyfin/Emby? Utilize tags in *arrs to control what content gets placeholders made and shown
- **Automation Ready**: Works with other tools in your stack:
   - *Radarr/Sonarr* for downloads and library management
   - *Plex*, *Jellyfin*, or *Emby* for streaming
   - *Overseerr* for requests (Optional)
     - Simply disable automatic search for requests
     - A placeholder will be made when the request gets added to arrs
     - User sees the title as a placeholder in Plex or Jellyfin and triggers the search when they play it
     - Saves you the storage space until the user is actually ready to watch
   - *Maintainerr* for storage management - Can be set up so when a real file is deleted, a placeholder is created to keep it visible in the media player (Optional)
     - Be sure to turn on the "On File Delete" trigger in your arrs webhook settings
     - Replaces content not being watched with a placeholder
     - Keeps content visible to users to re-download when they are ready to watch


---

## Key Features

- **Multi-Server Support:**  
  Works seamlessly with Plex, Jellyfin, and Emby. All placeholder, status, and automation features are available for all three platforms.
- **Automatic Placeholder Creation:**  
  Creates lightweight dummy video files for missing movies and TV episodes, so users can see and request unavailable content immediately.
- **Onboarding-First Configuration:**  
  First-time setup walks you through media server setup, *arr integrations, and library paths.
- **Calendar-Based Status Sync:**  
  Periodically syncs with Sonarr/Radarr calendars to create placeholders and update statuses for upcoming content (e.g., "Coming Soon", "Request").
- **Status in Summary/Description:**  
  Statuses (e.g., "Coming Soon", "Request", "Searching...") are prepended to the summary/description field for both movies and TV episodes, ensuring visibility in all media server clients (including mobile).
- **Queue Monitoring:**  
  Tracks download/search progress and updates status across all connected media servers as content moves through the queue.
- **Highly Configurable:**  
  Supports lookahead windows, "Coming Soon" toggles, preferred movie date types, multiple *arr instances, playback-driven downloads, and more.


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

Placeholdarr supports creating placeholders in dedicated folders or profiles. You'll configure these during onboarding or in the Settings dashboard.

During onboarding, you can define:
- **Standard Library** (movies/TV placeholder folders)
- **4K Library** (optional, for 4K-specific placeholders)
- **Anime Library** (optional, for anime profiles)

Once configured, you have two library setup options in your media server:

#### Option 1: Separate Libraries (Recommended)
- Add your placeholder folder(s) as a separate library in Plex/Jellyfin/Emby (for example, a "Requests" library).
- Keep your real file libraries (e.g., `/mnt/user/data/movies`, `/mnt/user/data/tv`) as separate libraries.
- Provides clarity to users about which items are placeholders (requests) versus available to play immediately.

#### Option 2: Combined Library
- Add both your placeholder folder(s) and your real file folder(s) to the same library in Plex/Jellyfin/Emby.
- Real files and placeholders appear together. Simpler to set up, but less visual separation.
- How media players handle trash may cause some issues when using this method.  

**Note:**  
Placeholdarr does not need to know your *arr root folders.Just configure the placeholder output paths during onboarding or in Settings.

---

### Configuration Overview

**Onboarding-First Approach:**
Placeholdarr starts with all general app settings (media server integrations, library paths, placeholder behavior) optional during first run. Complete the onboarding wizard in the UI to configure these. You can reconfigure them anytime in the Settings dashboard.

**Infrastructure-Only Environment Variables:**
The `.env` file is reserved for infrastructure/technical settings (database, server bind, logging). General app behavior is managed entirely through the dashboard.

**For first-run setup, you'll configure:**
- At least one media server (Plex, Jellyfin, or Emby)
- At least one *arr integration (Radarr and/or Sonarr)
- Placeholder output paths (library folders)
- Dummy video file paths (optional; defaults are provided)

**Optional Environment Variables (Infrastructure/Technical):
- `APPDATA_PATH`: In-container app data root used for logging and state (default `/config`)
- `LOG_LEVEL`: Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`)
- `LOG_DIR`: Directory for per-run log files (default `${APPDATA_PATH}/logs`)
- `LOG_FILE`: Full path to log directory (overrides `LOG_DIR` when set)
- `LOG_MAX_RUN_FILES`: Maximum number of per-run log files to keep (default `10`)
- `PLACEHOLDARR_HOST`: Server bind address (default `0.0.0.0`)
- `PLACEHOLDARR_PORT`: Server port (default `8000`)
- `RADARR_STD_INSTANCE_KEY`, `RADARR_4K_INSTANCE_KEY`, `SONARR_STD_INSTANCE_KEY`, `SONARR_4K_INSTANCE_KEY`: Optional webhook/sync instance key overrides (defaults: `radarr_std`, `radarr_4k`, `sonarr_std`, `sonarr_4k`)
- `TAUTULLI_INSTANCE_KEY`, `JELLYFIN_INSTANCE_KEY`, `EMBY_INSTANCE_KEY`: Optional playback webhook source key overrides (defaults: `tautulli`, `jellyfin`, `emby`)
- `ARR_INSTANCES_JSON`: Advanced JSON array for many named *arr instances (overrides fixed std/4K pairs when set)
  - Example: `[ {"arr_type":"radarr","instance_key":"radarr_main","label":"Main Radarr","url":"http://radarr:7878/api/v3","api_key":"...","is_4k":false}, {"arr_type":"sonarr","instance_key":"sonarr_anime","label":"Anime Sonarr","url":"http://sonarr-anime:8989/api/v3","api_key":"...","is_4k":false} ]`
- `PLACEHOLDER_FILE_MODE`: Octal file mode applied to placeholder files (default `666`)
- `PLACEHOLDER_DIR_MODE`: Octal directory mode applied to placeholder folders (default `777`)
- `FORCE_PRIME_ON_STARTUP`: Force a full content sync on app startup (`true`/`false`, default `false`)

**Dashboard-Managed Settings (Onboarding):**
All other behavior settings (placeholder strategy, status updates, TV play mode, calendar lookahead, playback handlers, etc.) are managed via the Settings dashboard. See the sections below for details on what each setting does.

---

### Placeholder Video Files

During onboarding, you'll configure dummy video file paths:
- **Standard Dummy File**: Used for available/requestable placeholders (default: `${APPDATA_PATH}/dummy.mp4`)
- **Coming Soon Dummy File**: (Optional) Used for "Coming Soon" placeholders for future releases. If empty, the standard dummy file is used for all placeholders (default: `${APPDATA_PATH}/coming_soon_dummy.mp4`)

You can change these paths anytime in Settings. Placeholdarr also creates `.nfo` sidecars beside each placeholder by default to support faster metadata refresh in media players.

---

### Disabling Media Servers

To disable a media server, go to **Settings** in the dashboard and clear the integration details:
- **Plex**: Clear the URL and API token fields
- **Jellyfin**: Clear the URL and API token fields
- **Emby**: Clear the URL and API token fields

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

Placeholdarr's playback event handlers are opt-in and configured in the Settings dashboard. After setting up at least one webhook source (Tautulli, Jellyfin, or Emby above), enable playback handlers in Settings to tune how searches are triggered:

| Setting | Default | Description |
|---|---|---|
| **Enable Playback Handlers** | Disabled | Activate playback-driven ARR searches |
| **Playback Search Preference** | Both | Which ARR instance to search for movies (`Standard`, `4K`, or `Both`) |
| **TV Playback Instance Mode** | Match | How to select Sonarr for TV: `Match` (path-based), `Preference` (follow above), or `Both` |
| **Playback Fallback Timeout** | 30 min | Minutes before a delayed fallback search fires if the preferred instance hasn't imported; `0` disables |
| **Playback Cooldown** | 30 sec | Seconds to suppress duplicate playback events for the same item; `0` disables |

**How it works:**
- **Placeholder played**: Placeholdarr immediately triggers an ARR search on the selected instance(s) based on your configured preferences.
- **Real file played**: If the alternate instance (standard vs. 4K) doesn't yet have the file, an optional fallback search can be triggered after the cooldown.
- **Fallback search**: If the preferred instance doesn't import within the configured timeout, Placeholdarr searches the alternate instance.
- **Skipped titles**: Content marked as deleted or ignored in Placeholdarr is never searched.

---

## Docker Usage Notes

**Dummy Video Files:**
When running Placeholdarr in Docker, you need to provide dummy video files for placeholders. You can:

1. **Use the sample file from the repository:**
   - Download `dummy.mp4` and `coming_soon_dummy.mp4` from the Placeholdarr GitHub repository and save it to your host (e.g., `/mnt/appdata/dummy.mp4`).

2. **Or create your own:**
   - Any small/valid video file works as a placeholder (e.g., a 100 KB video clip).

3. **Mount into Docker:**
   - In your `docker-compose.yml`, add a mount for your dummy files:
     ```yaml
     volumes:
       - /mnt/appdata/dummy.mp4:/config/dummy.mp4
       - /mnt/appdata/coming_soon_dummy.mp4:/config/coming_soon_dummy.mp4  # optional
     ```

4. **Configure in Onboarding:**
   - During first-run onboarding or in Settings, set the **Standard Dummy File** path to `/config/dummy.mp4`
   - Optionally set **Coming Soon Dummy File** to `/config/coming_soon_dummy.mp4`
   - Default paths (`/config/dummy.mp4` and `/config/coming_soon_dummy.mp4`) are auto-detected if files exist

---

## Current Capabilities

- **Multi-Server Support**: Plex, Jellyfin, and Emby with identical feature parity
- **4K Support**: Configure separate standard and 4K instances for Radarr/Sonarr
- **TV Play Modes**: Choose between episode, season, or series-level downloads
- **Flexible Placeholder Creation**: Hardlink or copy based on your storage setup
- **Status Tracking**: Real-time status updates (Searching, Coming Soon, Available) in media server descriptions
- **Calendar-Based Scheduling**: Automatic placeholder creation for upcoming releases based on configurable lookahead windows
- **Playback-Driven Downloads**: Optional automatic searches triggered when users play placeholders
- **Webhook Integration**: Works with Radarr, Sonarr, Tautulli, Jellyfin, and Emby
- **Cleanup Automation**: Automatically removes placeholders when real files become available
- **Multi-Instance *arrs**: Support for multiple Radarr/Sonarr instances via advanced JSON configuration

## Troubleshooting

**Onboarding Issues:**
- **Can't access the dashboard**: Verify the server is running and check `http://localhost:8000` (or your configured address/port). Check logs in `/config/logs/` for startup errors.
- **Settings not saving**: Ensure the database is initialized correctly. Check logs for database connection errors.

**Placeholder Creation Issues:**
- **No placeholders appearing**: Verify at least one media server is configured in Settings. Check that library paths are correct. Review logs for errors during placeholder creation.
- **Missing dummy video**: Verify dummy file paths in Settings point to valid video files. Defaults look for `/config/dummy.mp4` and `/config/coming_soon.mp4`.

**Webhook Issues:**
- **Webhooks not triggering**: Double-check the webhook URL format in *arr/Tautulli settings. Verify the instance key parameter matches your configured value. Check logs for webhook delivery errors.
- **Download not starting**: Verify *arr API keys are correct in Settings. Confirm *arr instances are reachable. Check logs for API errors.

**Performance Issues:**
- **Slow status updates**: Adjust `CALENDAR_SYNC_INTERVAL_HOURS` in `.env` if needed (defaults to reasonable values). Large libraries may take longer for initial sync.
- **High CPU usage**: Check logs for stuck jobs. Consider adjusting `CHECK_INTERVAL` or `FULL_SYNC_INTERVAL_HOURS` if too aggressive.

**For detailed debugging**: Check application logs in `/config/logs/` and enable `DEBUG` log level in Settings or set `LOG_LEVEL=DEBUG` in `.env` for more verbose output.
