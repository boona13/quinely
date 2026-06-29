---
name: webhooks
description: "Create and manage webhook triggers that fire Quinely actions when external services send HTTP POST events"
triggers:
  - webhook
  - webhooks
  - trigger
  - github webhook
  - stripe webhook
  - ci webhook
  - event trigger
  - http trigger
  - webhook create
  - webhook delete
tools:
  - webhook_create
  - webhook_list
  - webhook_delete
  - webhook_test
  - memory_search
priority: 55
---

# Webhook Triggers — Event-Driven Autonomy

Webhook triggers let external services (GitHub, CI, Stripe, custom apps) fire Quinely actions in real-time via HTTP POST. Each trigger has a pre-defined prompt template populated from the event payload — the webhook sender cannot inject arbitrary instructions.

## Available Tools

| Tool | Purpose |
|------|---------|
| `webhook_create` | Create a new trigger (from template or custom prompt) |
| `webhook_list` | List all configured triggers with URLs and status |
| `webhook_delete` | Remove a trigger by ID |
| `webhook_test` | Simulate a webhook event to test the full dispatch pipeline |

## Workflow

### Creating a trigger from a built-in template
1. `webhook_create(name="GitHub Push Review", template_id="github_push")`
2. Share the returned URL with the external service
3. Configure the external service to POST with `Authorization: Bearer <webhook_secret>`

### Creating a custom trigger
1. `webhook_create(name="Deploy Notifier", prompt_template="A deployment to {env} completed with status {status}. Review the deployment and notify if issues.", extract_fields={"env": "environment", "status": "deploy_status"})`

### Testing a trigger
1. `webhook_test(trigger_id="github-push-review")` — fires with a default test payload
2. `webhook_test(trigger_id="github-push-review", payload={"repository": {"full_name": "user/repo"}, "ref": "refs/heads/main"})` — with custom payload

## Built-in Templates

| Template ID | Name | Extracted Fields |
|-------------|------|-----------------|
| `github_push` | GitHub Push | repository, branch, pusher, commit_count, commits |
| `github_pr` | GitHub Pull Request | repository, action, pr_number, pr_title, author, branches, body |
| `github_issue` | GitHub Issue | repository, action, issue_number, issue_title, author, body |
| `generic` | Generic Webhook | event_type, payload_summary |

## Security

- **Bearer token auth**: Every request requires `Authorization: Bearer <webhook_secret>` header
- **HMAC verification**: Optional per-trigger HMAC signature check (e.g. GitHub's `X-Hub-Signature-256`)
- **Cooldown**: Per-trigger rate limiting (default 30s between fires)
- **Concurrency**: Global limit on simultaneous webhook dispatches (default 3)
- **Safe tool subset**: Webhook handlers cannot access evolve tools

## Key Rules

- Always verify the `webhook_secret` is configured before creating triggers
- Use `webhook_list` to check existing triggers before creating duplicates
- Use templates for common providers (GitHub, etc.) — they handle payload extraction correctly
- Custom triggers need `extract_fields` mapping template variables to dot-notation payload paths

## Extract Fields Syntax

- `key.subkey` — nested object access: `repository.full_name`
- `items[].name` — array iteration: `commits[].message` (joins with newlines)
- `items.#len` — array length: `commits.#len`
