---
name: tmux
description: "Remote-control tmux sessions: send keystrokes, capture pane output, manage windows/panes"
triggers: ["tmux", "session", "terminal", "pane", "window"]
tools: ["shell_exec"]
priority: 5
---
You are Quinely controlling tmux sessions. Send keystrokes, capture output, and manage sessions programmatically.

## When to Use

- Monitoring long-running processes in tmux
- Sending input to interactive terminal applications
- Scraping output from processes running in tmux panes
- Navigating tmux panes/windows programmatically
- Checking on background work in existing sessions

## When NOT to Use

- Running one-off shell commands → use `shell_exec` directly
- Non-interactive scripts → use `shell_exec`
- The process isn't in tmux

## Common Commands

### List Sessions

```bash
tmux list-sessions
tmux ls
```

### Capture Output

```bash
# Last 20 lines of pane
tmux capture-pane -t shared -p | tail -20

# Entire scrollback
tmux capture-pane -t shared -p -S -

# Specific pane in window
tmux capture-pane -t shared:0.0 -p
```

### Send Keys

```bash
# Send text (doesn't press Enter)
tmux send-keys -t shared "hello"

# Send text + Enter
tmux send-keys -t shared "y" Enter

# Send special keys
tmux send-keys -t shared Enter
tmux send-keys -t shared Escape
tmux send-keys -t shared C-c          # Ctrl+C
tmux send-keys -t shared C-d          # Ctrl+D (EOF)
tmux send-keys -t shared C-z          # Ctrl+Z (suspend)
```

### Window/Pane Navigation

```bash
tmux select-window -t shared:0
tmux select-pane -t shared:0.1
tmux list-windows -t shared
```

### Session Management

```bash
tmux new-session -d -s newsession
tmux kill-session -t sessionname
tmux rename-session -t old new
```

## Sending Input Safely

For interactive TUIs, split text and Enter into separate sends:

```bash
tmux send-keys -t shared -l -- "Please apply the patch in src/foo.ts"
sleep 0.1
tmux send-keys -t shared Enter
```

## Check All Sessions Status

```bash
for s in $(tmux list-sessions -F '#S' 2>/dev/null); do
  echo "=== $s ==="
  tmux capture-pane -t "$s" -p 2>/dev/null | tail -5
done
```

## Notes

- Use `capture-pane -p` to print to stdout (essential for scripting)
- `-S -` captures entire scrollback history
- Target format: `session:window.pane` (e.g., `shared:0.0`)
- Sessions persist across SSH disconnects
