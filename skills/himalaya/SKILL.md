---
name: himalaya
description: "CLI email client via IMAP/SMTP: list, read, write, reply, forward, search, and organize emails"
triggers: ["email", "mail", "inbox", "himalaya", "send email", "reply"]
tools: ["shell_exec", "file_read", "file_write"]
priority: 5
---
You are Quinely managing email. Use `himalaya` to manage emails from the terminal via IMAP/SMTP.

## Prerequisites

1. Himalaya CLI installed (`himalaya --version` to verify)
2. A configuration file at `~/.config/himalaya/config.toml`
3. IMAP/SMTP credentials configured

## Setup

Run the interactive wizard:

```bash
himalaya account configure
```

Or create `~/.config/himalaya/config.toml` manually:

```toml
[accounts.personal]
email = "you@example.com"
display-name = "Your Name"
default = true

backend.type = "imap"
backend.host = "imap.example.com"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "you@example.com"
backend.auth.type = "password"
backend.auth.cmd = "pass show email/imap"

message.send.backend.type = "smtp"
message.send.backend.host = "smtp.example.com"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "you@example.com"
message.send.backend.auth.type = "password"
message.send.backend.auth.cmd = "pass show email/smtp"
```

## Common Operations

### List Folders

```bash
himalaya folder list
```

### List Emails

```bash
himalaya envelope list                    # INBOX (default)
himalaya envelope list --folder "Sent"    # Specific folder
himalaya envelope list --page 1 --page-size 20  # Paginated
```

### Search Emails

```bash
himalaya envelope list from john@example.com subject meeting
```

### Read an Email

```bash
himalaya message read 42               # Plain text
himalaya message export 42 --full      # Raw MIME
```

### Reply / Forward

```bash
himalaya message reply 42              # Reply
himalaya message reply 42 --all        # Reply-all
himalaya message forward 42            # Forward
```

### Write a New Email

Interactive compose (opens $EDITOR):

```bash
himalaya message write
```

Send directly using template:

```bash
cat << 'EOF' | himalaya template send
From: you@example.com
To: recipient@example.com
Subject: Test Message

Hello from Himalaya!
EOF
```

Or with headers flag:

```bash
himalaya message write -H "To:recipient@example.com" -H "Subject:Test" "Message body here"
```

### Move/Copy/Delete Emails

```bash
himalaya message move 42 "Archive"
himalaya message copy 42 "Important"
himalaya message delete 42
```

### Manage Flags

```bash
himalaya flag add 42 --flag seen
himalaya flag remove 42 --flag seen
```

## Multiple Accounts

```bash
himalaya account list
himalaya --account work envelope list
```

## Attachments

```bash
himalaya attachment download 42
himalaya attachment download 42 --dir ~/Downloads
```

## Output Formats

```bash
himalaya envelope list --output json
himalaya envelope list --output plain
```

## Debugging

```bash
RUST_LOG=debug himalaya envelope list
RUST_LOG=trace RUST_BACKTRACE=1 himalaya envelope list
```

## Notes

- Install: `brew install himalaya`
- Message IDs are relative to the current folder; re-list after folder changes.
- Store passwords securely using `pass`, system keyring, or a command that outputs the password.
