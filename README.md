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

- **Jellyfin Support:**  
  Placeholdarr now works with Jellyfin as well as Plex. All placeholder, status, and automation features are available for both servers.
- **Automatic Placeholder Creation:**  
  Creates dummy video files for missing movies and TV episodes, so users can see and request unavailable content in Plex or Jellyfin.
- **Calendar-Based Status Sync:**  
  Periodically syncs with Sonarr/Radarr calendars to create placeholders and update statuses for upcoming content (e.g., "Coming Soon", "Request").
- **Batch Processing:**  
  Efficiently batches placeholder creation and library refreshes to avoid missed updates and improve performance.
- **Status in Summary/Description:**  
  Statuses (e.g., "Coming Soon", "Request", "Searching...") are now prepended to the summary/description field for both movies and TV episodes, ensuring visibility in all Plex and Jellyfin clients (including mobile).
- **Queue Monitoring:**  
  Tracks download/search progress and updates status in Plex or Jellyfin as content moves through the queue.
- **Highly Configurable:**  
  Supports lookahead windows, "Coming Soon" toggles, preferred movie date types, and more via `.env` settings.
- **Robust Logging:**  
  Emoji-enhanced logs for easy filtering and debugging.

---

## Configuration

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

Required settings in `.env`:
- `PLEX_URL`, `PLEX_TOKEN`: Your Plex server details
- `RADARR_URL`, `RADARR_API_KEY`: Radarr connection details
- `SONARR_URL`, `SONARR_API_KEY`: Sonarr connection details
- `DUMMY_MOVIE_LIBRARY_FOLDER`, `DUMMY_TV_LIBRARY_FOLDER`: Folders where placeholders (and optionally real files) will be created and scanned by Plex/Jellyfin
- `DUMMY_FILE_PATH`: Path to your dummy.mp4 file

Optional settings:
- `PLACEHOLDER_STRATEGY`: How to create placeholders (`hardlink` or `copy`)
- `TV_PLAY_MODE`: Download scope (`episode`, `season`, or `series`)
- `TITLE_UPDATES`: What level of status updates are shown in Plex. "ALL" not currently recommended, as this feature is still in development (`OFF`, `REQUEST`, `ALL`)
- 4K support settings (if needed)
- `INCLUDE_SPECIALS`: Include specials in TV placeholder creation (`true`/`false`)
- `EPISODES_LOOKAHEAD`: Number of episodes to look ahead and download (integer)
- `MAX_MONITOR_TIME`: Maximum time to monitor for file in seconds (integer)
- `CHECK_INTERVAL`: How often to check queue status in seconds (integer)
- `AVAILABLE_CLEANUP_DELAY`: Delay before removing monitored item after it becomes available (integer)
- **Calendar-based status update settings:**
  - `CALENDAR_LOOKAHEAD_DAYS`: How many days into the future to allow placeholders/"Coming Soon" (integer)
  - `CALENDAR_SYNC_INTERVAL_HOURS`: How often to sync calendar and update statuses (hours, integer)
  - `ENABLE_COMING_SOON_PLACEHOLDERS`: Enable or disable "Coming Soon" placeholders (`true`/`false`)
  - `PREFERRED_MOVIE_DATE_TYPE`: Which movie release date to use (`inCinemas`, `digitalRelease`, `physicalRelease`)
  - `ENABLE_COMING_SOON_COUNTDOWN`: Show countdown in "Coming Soon" status (`true`/`false`)
  - `CALENDAR_PLACEHOLDER_MODE`: Add placeholders as each episode enters lookahead window (`episode`) or add all known episodes of a season when any enters window (`season`)

---

### Placeholder Video Files

- `DUMMY_FILE_PATH`: Path to your standard dummy video file (used for available/requestable placeholders).
- `COMING_SOON_DUMMY_FILE_PATH`: (Optional) Path to a special dummy video file used for "Coming Soon" placeholders (future releases).  
  If not set, the standard dummy file will be used for all placeholders.

---

### Disabling Plex or Jellyfin

- **To disable Plex:**  
  Leave `PLEX_URL`, `PLEX_TOKEN`, `PLEX_MOVIE_SECTION_ID`, and `PLEX_TV_SECTION_ID` blank in your `.env` file.

- **To disable Jellyfin:**  
  Leave `JELLYFIN_URL` and `JELLYFIN_TOKEN` blank in your `.env` file.

**Note:**  
You must have at least one of Plex or Jellyfin configured. Placeholdarr will automatically detect which server(s) to use based on which variables are set.

---

### Tautulli (Plex) Webhook Setup

1. In Tautulli, go to Settings → Notification Agents
2. Add a new Webhook notification agent
3. Configure the webhook:
   - Webhook URL: `http://your-server:8000/webhook`
   - Trigger: Playback Start
   - Payload Format: JSON
4. Put in this JSON payload under the Playback Start JSON in the Data tab:
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

1. In Jellyfin, go to **Dashboard → Plugins → Catalog** and install the **Webhook** plugin if not already installed.
2. Go to **Dashboard → Plugins → Webhook** and click **Add Webhook**.
3. Set the **Webhook URL** to:
   ```
   http://your-server:8000/webhook
   ```
   Replace `your-server` with your actual server address.
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

**Note:** Placeholdarr will automatically detect and process both Tautulli (Plex) and Jellyfin webhooks using their respective payload formats. No extra configuration is needed—just set up the webhook as shown.

---

### Radarr Webhook Setup

- For more-tailored control of content, utilize tags to determine what titles get placeholders created for them. 

1. In Radarr, go to Settings → Connect → Add Connection (Plus Icon)
2. Select "Webhook"
3. Configure:
   - Name: PlaceholdARR
   - URL: `http://your-server:8000/webhook`
   - Method: POST
   - Triggers (enable only):
     - On Import
     - On Movie Added
     - On Movie Delete
     - On Movie File Delete

---

### Sonarr Webhook Setup

1. In Sonarr, go to Settings → Connect → Add Connection (Plus Icon)
2. Select "Webhook"
3. Configure:
   - Name: PlaceholdARR
   - URL: `http://your-server:8000/webhook`
   - Method: POST
   - Triggers (enable only):
     - On Import
     - On Series Add
     - On Series Delete
     - On Episode File Delete

---


## Docker Usage Notes

**IMPORTANT:** For Plex/Tautulli compatibility, all path values in your `.env` file (such as `MOVIE_LIBRARY_FOLDER`, `TV_LIBRARY_FOLDER`, `DUMMY_FILE_PATH`, etc.) **must match the absolute host path** (the left side of your Docker volume mounts), and you must mount the host path to the **same absolute path inside the container** (host path : host path). This is because Tautulli sends the host path in its webhook payload, and Placeholdarr must match it to identify placeholders.

If you are running Placeholdarr in Docker, you **must provide a `dummy.mp4` file** on the host and mount it into the container at the same absolute path.
You can use the sample `dummy.mp4` provided in this repository, or supply your own small/valid video file if you prefer.

**Instructions:**
1. **Download the sample dummy files:**  
  - Download both `dummy.mp4` and `coming_soon_dummy.mp4` from the Placeholdarr GitHub repository and save them to your host (e.g., `/mnt/plex/Movies/placeholders/dummy.mp4` and `/mnt/plex/Movies/placeholders/coming_soon_dummy.mp4`).
2. **Or use your own:**  
  - You may use any small/valid video files as placeholders.
3. **Mount both files into the container at the same absolute path:**  
  - In your `docker-compose.yml` or `docker run` command, mount them as:
    ```yaml
    volumes:
     - /mnt/plex/Movies/placeholders:/mnt/plex/Movies/placeholders
     - /mnt/plex/Movies/placeholders/dummy.mp4:/mnt/plex/Movies/placeholders/dummy.mp4
     - /mnt/plex/Movies/placeholders/coming_soon_dummy.mp4:/mnt/plex/Movies/placeholders/coming_soon_dummy.mp4
    ```
4. **Set the paths in your `.env`:**  
  - `DUMMY_FILE_PATH=/mnt/plex/Movies/placeholders/dummy.mp4`
  - `COMING_SOON_DUMMY_FILE_PATH=/mnt/plex/Movies/placeholders/coming_soon_dummy.mp4`
  - Both files must be present and mounted, but the `COMING_SOON_DUMMY_FILE_PATH` will only be used if the related setting is enabled in your `.env` (e.g., `ENABLE_COMING_SOON_PLACEHOLDERS=true`).

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
