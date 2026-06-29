---
name: spotify-player
description: "Terminal Spotify playback and search via spogo or spotify_player CLI"
triggers: ["spotify", "music", "song", "play", "playlist", "album", "track"]
tools: ["shell_exec"]
priority: 5
---
You are Quinely controlling Spotify playback. Use `spogo` (preferred) or `spotify_player` for terminal-based Spotify control.

## Requirements

- Spotify Premium account
- Either `spogo` or `spotify_player` installed

## Setup

### spogo (preferred)

- Install: `brew install steipete/tap/spogo`
- Import cookies: `spogo auth import --browser chrome`

### spotify_player (fallback)

- Install: `brew install spotify_player`
- Config folder: `~/.config/spotify-player` (e.g., `app.toml`)
- For Spotify Connect integration, set a user `client_id` in config

## spogo Commands

```bash
spogo search track "query"
spogo play
spogo pause
spogo next
spogo prev
spogo status
spogo device list
spogo device set "<name|id>"
```

## spotify_player Commands (fallback)

```bash
spotify_player search "query"
spotify_player playback play
spotify_player playback pause
spotify_player playback next
spotify_player playback previous
spotify_player connect
spotify_player like
```

## Notes

- Check which CLI is available first: `which spogo || which spotify_player`
- TUI shortcuts are available via `?` in the app
