---
name: peekaboo
description: "macOS UI automation via Peekaboo CLI: capture screens, click elements, type text, manage apps/windows"
triggers:
  - peekaboo
  - ui
  - window
  - screen
  - screenshot
  - click
  - automate
  - automation
tools: ["shell_exec", "file_read"]
priority: 5
---
You are Quinely automating macOS UI with Peekaboo. Use the `peekaboo` CLI for screen capture, UI element targeting, input automation, and app/window management.

## Quick Start

```bash
peekaboo permissions
peekaboo list apps --json
peekaboo see --annotate --path /tmp/peekaboo-see.png
peekaboo click --on B1
peekaboo type "Hello" --return
```

## Features

### Core

- `capture`: live capture or video ingest + frame extraction
- `image`: capture screenshots (screen/window/menu bar regions)
- `list`: apps, windows, screens, menubar, permissions
- `see`: annotated UI maps, snapshot IDs, optional analysis
- `permissions`: check Screen Recording/Accessibility status
- `clean`: prune snapshot cache and temp files

### Interaction

- `click`: target by ID/query/coords with smart waits
- `drag`: drag & drop across elements/coords/Dock
- `hotkey`: modifier combos like `cmd,shift,t`
- `move`: cursor positioning with optional smoothing
- `paste`: set clipboard -> paste -> restore
- `press`: special-key sequences with repeats
- `scroll`: directional scrolling (targeted + smooth)
- `swipe`: gesture-style drags between targets
- `type`: text + control keys (`--clear`, delays)

### System

- `app`: launch/quit/relaunch/hide/unhide/switch/list apps
- `clipboard`: read/write clipboard (text/images/files)
- `dialog`: click/input/file/dismiss/list system dialogs
- `dock`: launch/right-click/hide/show/list Dock items
- `menu`: click/list application menus + menu extras
- `menubar`: list/click status bar items
- `window`: close/minimize/maximize/move/resize/focus/list
- `space`: list/switch/move-window (Spaces)

## Common Targeting Parameters

- App/window: `--app`, `--pid`, `--window-title`, `--window-id`, `--window-index`
- Snapshot targeting: `--snapshot` (ID from `see`; defaults to latest)
- Element/coords: `--on`/`--id` (element ID), `--coords x,y`

## Examples

### See -> Click -> Type (most reliable flow)

```bash
peekaboo see --app Safari --window-title "Login" --annotate --path /tmp/see.png
peekaboo click --on B3 --app Safari
peekaboo type "user@example.com" --app Safari
peekaboo press tab --count 1 --app Safari
peekaboo type "supersecret" --app Safari --return
```

### Target by Window ID

```bash
peekaboo list windows --app "Visual Studio Code" --json
peekaboo click --window-id 12345 --coords 120,160
peekaboo type "Hello from Peekaboo" --window-id 12345
```

### Capture Screenshots + Analyze

```bash
peekaboo image --mode screen --screen-index 0 --retina --path /tmp/screen.png
peekaboo image --app Safari --window-title "Dashboard" --analyze "Summarize KPIs"
peekaboo see --mode screen --screen-index 0 --analyze "Summarize the dashboard"
```

### App + Window Management

```bash
peekaboo app launch "Safari" --open https://example.com
peekaboo window focus --app Safari --window-title "Example"
peekaboo window set-bounds --app Safari --x 50 --y 50 --width 1200 --height 800
peekaboo app quit --app Safari
```

### Menus, Menubar, Dock

```bash
peekaboo menu click --app Safari --item "New Window"
peekaboo menu click --app TextEdit --path "Format > Font > Show Fonts"
peekaboo menu click-extra --title "WiFi"
peekaboo dock launch Safari
peekaboo menubar list --json
```

### Mouse + Gesture Input

```bash
peekaboo move 500,300 --smooth
peekaboo drag --from B1 --to T2
peekaboo swipe --from-coords 100,500 --to-coords 100,200 --duration 800
peekaboo scroll --direction down --amount 6 --smooth
```

### Keyboard Input

```bash
peekaboo hotkey --keys "cmd,shift,t"
peekaboo press escape
peekaboo type "Line 1\nLine 2" --delay 10
```

## Setup

- Install: `brew install steipete/tap/peekaboo`
- Requires Screen Recording + Accessibility permissions.
- Use `peekaboo see --annotate` to identify targets before clicking.
