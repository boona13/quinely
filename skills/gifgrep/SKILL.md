---
name: gifgrep
description: "Search GIF providers (Tenor/Giphy), download, and extract stills/sheets via gifgrep CLI"
triggers: ["gif", "meme", "reaction", "gifgrep", "tenor", "giphy"]
tools: ["shell_exec", "clipboard_write"]
priority: 5
---
You are Quinely finding GIFs. Use `gifgrep` to search GIF providers (Tenor/Giphy), browse in a TUI, download results, and extract stills or sheets.

## Quick Start

```bash
gifgrep cats --max 5
gifgrep cats --format url | head -n 5
gifgrep search --json cats | jq '.[0].url'
gifgrep tui "office handshake"
gifgrep cats --download --max 1 --format url
```

## TUI + Previews

- TUI: `gifgrep tui "query"`
- CLI still previews: `--thumbs` (Kitty/Ghostty only; still frame)

## Download + Reveal

- `--download` saves to `~/Downloads`
- `--reveal` shows the last download in Finder

## Stills + Sheets

```bash
gifgrep still ./clip.gif --at 1.5s -o still.png
gifgrep sheet ./clip.gif --frames 9 --cols 3 -o sheet.png
```

Sheets = single PNG grid of sampled frames (great for quick review, docs, PRs, chat).
Tune: `--frames` (count), `--cols` (grid width), `--padding` (spacing).

## Providers

- `--source auto|tenor|giphy`
- `GIPHY_API_KEY` required for `--source giphy`
- `TENOR_API_KEY` optional (Tenor demo key used if unset)

## Output

- `--json` prints an array of results (`id`, `title`, `url`, `preview_url`, `tags`, `width`, `height`)
- `--format` for pipe-friendly fields (e.g., `url`)

## Setup

- Install: `brew install steipete/tap/gifgrep`
- Or: `go install github.com/steipete/gifgrep/cmd/gifgrep@latest`
