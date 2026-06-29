---
name: apple-reminders
description: "Manage Apple Reminders via remindctl CLI (list, add, edit, complete, delete). Syncs to iOS."
triggers: ["reminder", "reminders", "todo", "remindctl", "due date"]
tools: ["shell_exec"]
priority: 5
---
You are Quinely managing Apple Reminders. Use `remindctl` to manage Apple Reminders directly from the terminal.

## When to Use

- User explicitly mentions "reminder" or "Reminders app"
- Creating personal to-dos with due dates that sync to iOS
- Managing Apple Reminders lists
- User wants tasks to appear in their iPhone/iPad Reminders app

## When NOT to Use

- Calendar events or appointments → use Apple Calendar
- Project/work task management → use Notion, GitHub Issues, etc.
- Things 3 tasks → use the things-mac skill

## Setup

- Install: `brew install steipete/tap/remindctl`
- macOS-only; grant Reminders permission when prompted
- Check status: `remindctl status`
- Request access: `remindctl authorize`

## Common Commands

### View Reminders

```bash
remindctl                    # Today's reminders
remindctl today              # Today
remindctl tomorrow           # Tomorrow
remindctl week               # This week
remindctl overdue            # Past due
remindctl all                # Everything
remindctl 2026-01-04         # Specific date
```

### Manage Lists

```bash
remindctl list               # List all lists
remindctl list Work          # Show specific list
remindctl list Projects --create    # Create list
remindctl list Work --delete        # Delete list
```

### Create Reminders

```bash
remindctl add "Buy milk"
remindctl add --title "Call mom" --list Personal --due tomorrow
remindctl add --title "Meeting prep" --due "2026-02-15 09:00"
```

### Complete/Delete

```bash
remindctl complete 1 2 3     # Complete by ID
remindctl delete 4A83 --force  # Delete by ID
```

### Output Formats

```bash
remindctl today --json       # JSON for scripting
remindctl today --plain      # TSV format
remindctl today --quiet      # Counts only
```

## Date Formats

Accepted by `--due` and date filters:

- `today`, `tomorrow`, `yesterday`
- `YYYY-MM-DD`
- `YYYY-MM-DD HH:mm`
- ISO 8601 (`2026-01-04T12:34:56Z`)

## Clarifying User Intent

User: "Remind me to check on the deploy in 2 hours"

**Ask:** "Do you want this in Apple Reminders (syncs to your phone) or as a Quinely notification?"

- Apple Reminders → use this skill
- Quinely notification → use the notify tool
