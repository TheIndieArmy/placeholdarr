# Placeholdarr

Placeholdarr is an AI-developed application conducted by TheIndieArmy and built from the ground up, inspired by [Infinite Plex Library](https://github.com/arjanterheegde/infiniteplexlibrary) and [Chronicle](https://github.com/iwouldratherbeatthebeach/chronicle/tree/main). 

## Overview

Placeholdarr bridges the gap between media discovery and storage management. It allows you to maintain a comprehensive Plex library without the storage overhead of keeping everything downloaded at once.

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
   - *Plex* for streaming
   - *Overseerr* for requests (Optional)
     - Simply disable automatic search for requests
     - A placeholder will be made when the request gets added to arrs
     - User sees the title as a placeholder in Plex and triggers the search when they play it
     - Saves you the storage space until the user is actually ready to watch
   - *Maintainerr* for storage management - Can be set up so when a real file is deleted, a placeholder is created to keep it visible in Plex (Optional)
     - Be sure to turn on the "On File Delete" trigger in your arrs webhook settings
     - Replaces content not being watched with a placeholder
     - Keeps content visible to users in Plex to re-download when they are ready to watch

Perfect for:
- Large libraries with limited storage
- Media servers with multiple users
- Automated media management setups
- Collections that exceed available storage

---

## Key Features

- **Automatic Placeholder Creation:**  
  Creates dummy video files for missing movies and TV episodes, so users can see and request unavailable content in Plex.
- **Calendar-Based Status Sync:**  
  Periodically syncs with Sonarr/Radarr calendars to create placeholders and update statuses for upcoming content (e.g., "Coming Soon", "Request").
- **Batch Processing:**  
  Efficiently batches placeholder creation and Plex refreshes to avoid missed updates and improve performance.
- **Status in Summary:**  
  Statuses (e.g., "Coming Soon", "Request", "Searching...") are now prepended to the summary/description field for both movies and TV episodes, ensuring visibility in all Plex clients (including mobile).
- **Queue Monitoring:**  
  Tracks download/search progress and updates status in Plex as content moves through the queue.
- **Highly Configurable:**  
  Supports lookahead windows, "Coming Soon" toggles, preferred movie date types, and more via `.env` settings.
- **Robust Logging:**  
  Emoji-enhanced logs for easy filtering and debugging.

---

## Configuration

### Environment Variables

Required settings in `.env`:
- `PLEX_URL`, `PLEX_TOKEN`: Your Plex server details
- `RADARR_URL`, `RADARR_API_KEY`: Radarr connection details
- `SONARR_URL`, `SONARR_API_KEY`: Sonarr connection details
- `MOVIE_LIBRARY_FOLDER`, `TV_LIBRARY_FOLDER`: Plex library paths
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

### Tautulli Webhook Setup

1. In Tautulli, go to Settings → Notification Agents
2. Add a new Webhook notification agent
3. Configure the webhook:
   - Webhook URL: `http://your-server:8000/webhook`
   - Trigger: Playback Start
   - Payload Format: JSON
   
4. Add this condition to only trigger on dummy files:
```
{
    "operator": "contains",
    "condition": "filename",
    "value": "dummy"
}
```

5. Use this JSON payload:
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
