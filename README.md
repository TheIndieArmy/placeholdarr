# Placeholdarr

**Placeholdarr** keeps titles visible in Plex\*, Jellyfin, or Emby without requiring everything to stay downloaded. It is AI-developed, maintained by TheIndieArmy, and inspired by [Infinite Plex Library](https://github.com/arjanterheegde/infiniteplexlibrary) and [Chronicle](https://github.com/iwouldratherbeatthebeach/chronicle).

\***Placeholdarr** with **Plex** requires **Tautulli** to function properly.

## Overview

Placeholdarr allows you to maintain a comprehensive **Plex**, Jellyfin, or Emby library without the storage overhead of keeping everything downloaded at once. It automates placeholder creation, status tracking, and on-demand downloads.

## Benefits & Features

- **Keep your library visible without downloading everything.**  
Placeholders let users browse and request content first, then download on demand.
- **Reduce storage churn while keeping content discoverable.**  
Great for large import lists, rolling retention workflows, and "watch-when-needed" setups.
- **Support modern multi-instance stacks.**  
Works with Plex, Jellyfin, Emby, and multiple Radarr/Sonarr instances.
- **Automate status and release workflows.**  
Startup sync, calendar workflows, queue tracking, and playback-driven search are built in.
- **Build Plex collections from recipes (Beta).**  
Sync catalog or list-driven membership into Plex on a schedule, including Collection Sets that fan out one config into many shelves.
- **Stay practical for real-world ops.**  
Hardlink/copy placeholder strategies, cleanup automation, and onboarding-first configuration.

## How It Works

1. Add content to Radarr/Sonarr without immediately searching/downloading.
2. Placeholdarr creates lightweight placeholder files in your configured placeholder libraries.
3. Users see those titles in Plex/Jellyfin/Emby like normal catalog items.
4. Playback or automation events trigger search/download flows, and placeholders are replaced when real media arrives.

## Getting Started

1. Run the included [docker-compose.yml](docker-compose.yml).
2. Open Placeholdarr and complete onboarding in the WebUI.
3. Configure ARR/media-server webhooks using the URLs shown in onboarding.

## Configuration Notes

Placeholdarr is onboarding-first: most behavior is configured in the WebUI.

- **Library strategy (recommended):**
  - Keep placeholder libraries separate from real-media libraries when possible.
  - Combined libraries are supported, but media-server trash behavior can be less predictable.
  - Placeholder output paths should be different from ARR root paths.
- **Plex placeholder libraries (metadata agents):** Placeholdarr writes sidecar **`.nfo`** files next to placeholders. In Plex, set each **placeholder** movie and TV library and select **Plex NFO Movies** and **Plex NFO Series** as the agent. 
- **Environment variables:** use `.env` primarily for infrastructure/runtime overrides (host/port/log level/database).

## Troubleshooting

- Check logs in `/config/logs/` for startup, webhook, and sync details.
- Log files under `/config/logs/` capture full detail (VERBOSE/DEBUG and above). Use the System Logs tab display filter to narrow what you read.

## Credits

- **Jellyfin support integration:** Thanks to [Priky-one](https://github.com/Priky-one) for implementing Jellyfin support.
- **GHCR Docker workflow support:** Thanks to [aves-omni](https://github.com/aves-omni) for GitHub Container Registry workflow integration.
