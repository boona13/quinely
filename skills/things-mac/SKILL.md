---
name: things-mac
description: "Manage Things 3 via the things CLI on macOS (add/update/search todos and projects)"
triggers: ["things", "task", "project", "things 3", "inbox", "today"]
tools: ["shell_exec"]
priority: 5
---
You are Quinely managing Things 3 tasks. Use `things` to read your local Things database and add/update todos via the Things URL scheme.

## Setup

- Install (Apple Silicon): `GOBIN=/opt/homebrew/bin go install github.com/ossianhempel/things3-cli/cmd/things@latest`
- If DB reads fail: grant **Full Disk Access** to the calling app (Terminal / your gateway app).
- Optional: set `THINGSDB` (or pass `--db`) to point at your `ThingsData-*` folder.
- Optional: set `THINGS_AUTH_TOKEN` to avoid passing `--auth-token` for update ops.

## Read-only (DB queries)

```bash
things inbox --limit 50
things today
things upcoming
things search "query"
things projects
things areas
things tags
```

## Write (URL scheme)

- Prefer safe preview: `things --dry-run add "Title"`
- Add: `things add "Title" --notes "..." --when today --deadline 2026-01-02`
- Bring Things to front: `things --foreground add "Title"`

### Add Examples

```bash
things add "Buy milk"
things add "Buy milk" --notes "2% + bananas"
things add "Book flights" --list "Travel"
things add "Pack charger" --list "Travel" --heading "Before"
things add "Call dentist" --tags "health,phone"
things add "Trip prep" --checklist-item "Passport" --checklist-item "Tickets"
```

### Multi-line from STDIN

```bash
cat <<'EOF' | things add -
Title line
Notes line 1
Notes line 2
EOF
```

## Modify a Todo (needs auth token)

1. Get the ID (UUID column): `things search "milk" --limit 5`
2. Auth: set `THINGS_AUTH_TOKEN` or pass `--auth-token <TOKEN>`

```bash
things update --id <UUID> --auth-token <TOKEN> "New title"
things update --id <UUID> --auth-token <TOKEN> --notes "New notes"
things update --id <UUID> --auth-token <TOKEN> --append-notes "..."
things update --id <UUID> --auth-token <TOKEN> --list "Travel" --heading "Before"
things update --id <UUID> --auth-token <TOKEN> --tags "a,b"
things update --id <UUID> --auth-token <TOKEN> --completed
things update --id <UUID> --auth-token <TOKEN> --canceled
```

- Safe preview: `things --dry-run update --id <UUID> --auth-token <TOKEN> --completed`

## Notes

- macOS-only.
- `--dry-run` prints the URL without opening Things.
- Delete is not supported by things3-cli; use `--completed` or `--canceled` instead.
