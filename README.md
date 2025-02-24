# Placeholdarr

Placeholdarr is an AI-developed application built from the ground up, inspired by [Infinite Plex Library](https://github.com/arjanterheegde/infiniteplexlibrary). 

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
   - The real content is automatically requested
   - Plex title updates show download progress
   - Placeholder is replaced with actual media when ready

### Integration Benefits

- **Storage Efficiency**: Only keep downloaded what's actively being watched
- **Full Library Visibility**: Users see everything available, not just downloaded content
- **Automation Ready**: Works with other tools in your stack:
  - Overseerr/Jellyseerr for requests
  - Maintainerr for storage management - When a real file is deleted, a placeholder is created to keep it visible in Plex
  - Radarr/Sonarr for downloads
  - Plex for streaming

Perfect for:
- Large libraries with limited storage
- Media servers with multiple users
- Automated media management setups
- Collections that exceed available storage

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
- 4K support settings (if needed)

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
        "series_title": "{series_title}",
        "episode_title": "{episode_title}",
        "season_num": "{season_num}",
        "episode_num": "{episode_num}",
        "year": "{year}",
        "ids": {
            "plex": "{rating_key}",
            "tmdb": "{tmdb_id}",
            "tvdb": "{thetvdb_id}",
            "imdb": "{imdb_id}"
        },
        "file_info": {
            "path": "{file}"
        }
    }
}
```

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

## Additional Features

- 4K Support: Configure separate 4K instances of Radarr/Sonarr
- TV Play Modes: Choose between episode/season/series downloads
- Hardlink/Copy: Choose how placeholder files are created
- Progress Tracking: Monitor downloads in Plex titles
- Auto Cleanup: Removes placeholders when downloads complete

## Troubleshooting

Common issues:
1. Port in use: Service cleans port 8000 on startup
2. Missing dummy.mp4: Create an empty file or small video
3. Webhook not triggering: Check Tautulli condition/payload
4. Download not starting: Verify *arr API keys and URLs
