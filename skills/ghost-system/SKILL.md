---
name: ghost-system
description: "Quinely's self-knowledge: complete system architecture, file map, design patterns, and modification rules"
triggers:
  - evolve
  - self-modify
  - add feature
  - new tool
  - new page
  - dashboard
  - ghost code
  - modify ghost
  - change ghost
  - update ghost
  - fix ghost
  - improve ghost
  - enhance ghost
  - ghost system
  - backend
  - frontend
  - blueprint
  - route
tools:
  - file_read
  - file_write
  - file_search
  - shell_exec
  - evolve_plan
  - evolve_apply
  - evolve_test
  - evolve_deploy
  - browser
priority: 90
---

# Quinely System Architecture — Self-Knowledge

You are modifying YOUR OWN codebase. You MUST understand every file you touch.
Read this ENTIRE section before writing any code.

## Project Root

`{project_dir}/` — all paths below are relative to this (the directory where Quinely is installed).

## Backend Python Modules

| File | Role | Key exports |
|------|------|-------------|
| `ghost.py` | Main daemon. CLI entry point, `GhostDaemon` class, tool registration, action processing, identity system (SOUL.md/USER.md), feed/action file I/O | `GhostDaemon`, `main()` |
| `ghost_loop.py` | Autonomous tool-loop engine. Multi-turn LLM+tool execution, loop detection, `task_complete` termination, debug logging | `ToolLoopEngine`, `ToolRegistry`, `LoopDetector`, `ToolLoopDebugLogger` |
| `ghost_tools.py` | Core tools: `shell_exec` (auto-syncs `requirements.txt` on `pip install`), `file_read`, `file_write`, `file_search`, `web_fetch`, `notify`, `app_control`, `uptime` | `build_default_tools(cfg)` |
| `ghost_browser.py` | Playwright browser automation — snapshot+ref pattern | `build_browser_tools()` |
| `ghost_memory.py` | SQLite FTS5 memory (save/search/prune) | `MemoryDB`, `make_memory_search()`, `make_memory_save()` |
| `ghost_hybrid_memory.py` | FTS5 + vector search combined | `build_hybrid_memory_tools()` |
| `ghost_cron.py` | Scheduled jobs (add/remove/run/enable/disable) | `CronService`, `build_cron_tools()` |
| `ghost_skills.py` | Skill discovery, trigger matching, prompt injection | `SkillLoader`, `parse_skill_md()` |
| `ghost_plugins.py` | Plugin system with hooks | `PluginLoader`, `HookRunner` |
| `ghost_evolve.py` | Self-modification engine (plan/apply/test/deploy/rollback) | `EvolutionEngine`, `build_evolve_tools()` |
| `ghost_autonomy.py` | Growth logging, action items, self-repair scheduling | `build_autonomy_tools()` |
| `ghost_integrations.py` | Google APIs (Gmail/Calendar/Drive/Docs/Sheets) + Grok/X | `build_integration_tools()` |
| `ghost_credentials.py` | Encrypted credential storage | `build_credential_tools()` |
| `ghost_x_tracker.py` | X/Twitter action dedup tracking (SQLite) | `build_x_tracker_tools()` |
| `ghost_code_intel.py` | AST-based code analysis | `build_code_intel_tools()` |
| `ghost_data_extract.py` | LLM-powered structured extraction | `build_data_extract_tools()` |
| `ghost_web_search.py` | Multi-provider web search with fallback | `build_web_search_tools()` |
| `ghost_supervisor.py` | Process supervisor — restarts, crash recovery, deploy handling | `GhostSupervisor` |

### Adding a New Backend Module

1. Create `ghost_<feature>.py` in project root
2. Export `build_<feature>_tools(cfg)` returning a list of tool dicts
3. Register in `ghost.py` `GhostDaemon.__init__()` — look for the tool registration block
4. Each tool dict: `{name, description, parameters, execute}`

### Tool Definition Pattern

```python
def build_my_tools(cfg):
    def _my_action(**kwargs):
        # implementation
        return "result string"

    return [{
        "name": "my_tool",
        "description": "What this tool does",
        "parameters": {
            "type": "object",
            "properties": {
                "arg1": {"type": "string", "description": "..."},
            },
            "required": ["arg1"]
        },
        "execute": _my_action
    }]
```

## Frontend Architecture

### Flask Dashboard (`ghost_dashboard/`)

| Path | Role |
|------|------|
| `__init__.py` | Flask app factory, `start_with_daemon()` |
| `templates/index.html` | SPA shell — loads Tailwind CDN, sidebar, mounts `#main-content` |
| `routes/__init__.py` | Registers all blueprint modules |
| `routes/chat.py` | Chat API: send, stream (SSE), status, clear, restart-recovery, upload |
| `routes/status.py` | `/api/status` — daemon health, features, tool list |
| `routes/config.py` | Config CRUD |
| `routes/models.py` | OpenRouter model browsing |
| `routes/identity.py` | SOUL.md / USER.md read/write |
| `routes/skills.py` | Skill list, enable/disable, edit |
| `routes/cron.py` | Cron CRUD |
| `routes/memory.py` | Memory search/prune |
| `routes/feed.py` | Activity feed |
| `routes/daemon.py` | Start/stop/pause/resume |
| `routes/evolve.py` | Evolution history, approve/reject, rollback |
| `routes/integrations.py` | Google OAuth, Grok/X config |
| `routes/autonomy.py` | Action items, growth log |
| `routes/webhooks.py` | Webhook trigger CRUD, fire endpoint, event history |
| `routes/setup.py` | First-run setup wizard |
| `routes/accounts.py` | Account management |

### JavaScript Pages (`static/js/pages/`)

Every page module exports `render(container)`. The SPA router in `app.js` calls it.

| File | Page | Hash route |
|------|------|-----------|
| `chat.js` | Quinely Chat | `#chat` |
| `overview.js` | System overview | `#overview` |
| `models.js` | Model browser | `#models` |
| `config.js` | Configuration | `#config` |
| `soul.js` | Soul editor | `#soul` |
| `user.js` | User profile | `#user` |
| `skills.js` | Skills manager | `#skills` |
| `cron.js` | Cron jobs | `#cron` |
| `memory.js` | Memory search | `#memory` |
| `feed.js` | Activity feed | `#feed` |
| `logs.js` | Debug logs | `#logs` |
| `evolve.js` | Evolution history | `#evolve` |
| `integrations.js` | Integrations | `#integrations` |
| `autonomy.js` | Autonomy/Growth | `#autonomy` |
| `webhooks.js` | Webhook Triggers | `#webhooks` |
| `accounts.js` | Accounts | `#accounts` |
| `setup.js` | Setup wizard | `#setup` |

### Core JS Files (`static/js/`)

| File | Role |
|------|------|
| `app.js` | SPA router, sidebar status poller (5s), `ghost:restarted` event, `navigate()`, `updateSidebarStatus()` |
| `api.js` | `window.GhostAPI` — `get()`, `post()`, `put()`, `patch()`, `postRaw()`, `del()` wrappers |
| `utils.js` | `window.GhostUtils` — `escapeHtml()`, `formatDate()`, `toast()`, `formatBytes()` |

### CSS (`static/css/dashboard.css`)

Single CSS file. Dark theme. Key class conventions:
- `.chat-*` — Chat page components
- `.model-card`, `.skill-card`, `.cron-card` — Card components
- `.nav-link`, `.nav-link.active` — Sidebar navigation
- `.btn`, `.btn-primary`, `.btn-ghost`, `.btn-danger`, `.btn-sm` — Buttons
- `.badge`, `.badge-*` — Status badges
- `.toggle-*` — Toggle switches
- `.chat-input-wrapper`, `.chat-send-btn`, `.chat-stop-btn` — Chat input area
- `.chat-att-*` — Attachment pills and upload UI
- `.chat-drop-zone`, `.drag-over` — Drag-and-drop area
- `.chat-restart-banner` — Restart indicator
- `.ghost-restart-pulse` — Pulsing animation for restart states

Color palette:
- Background: `#0a0a14` (darkest), `#10101c` (cards), `#161625` (inputs)
- Borders: `rgba(30, 30, 48, 0.5)`
- Quinely purple: `#8b5cf6` (primary), `#a78bfa` (hover), `rgba(139, 92, 246, *)` (glows)
- Text: `#ffffff` (headings), `#d4d4d8` (body), `#a1a1aa` (muted), `#71717a` (dim)
- Success: `#10b981` / `#34d399`
- Warning: `#f59e0b` / `#fbbf24`
- Error: `#ef4444` / `#f87171`

### Adding a New Dashboard Page

1. Create `ghost_dashboard/routes/<page>.py` — Flask blueprint with API endpoints
2. Register the blueprint in `routes/__init__.py`
3. Create `ghost_dashboard/static/js/pages/<page>.js` — export `render(container)`
4. Add route entry in `app.js` `navigate()` function
5. Add sidebar link in `templates/index.html`
6. Add styles to `dashboard.css` — follow existing naming: `.<page>-*`
7. **IMPORTANT**: Use patches in `evolve_apply` for ALL existing files

## Evolve Pipeline — How Self-Modification Works

```
evolve_plan(description, files) → creates backup, assigns ID
    ↓
evolve_apply(evolution_id, file_path, patches=[...]) → applies code changes
    ↓  (repeat for each file)
evolve_test(evolution_id) → syntax + import + smoke test
    ↓
evolve_deploy(evolution_id) → writes deploy marker → supervisor restarts Quinely
```

### evolve_apply Rules (CRITICAL)

- **Existing files**: MUST use `patches` mode — list of `{old: "...", new: "..."}` search/replace pairs
- **New files only**: May use `content` mode for full file content
- **NEVER use `content` mode on existing .py, .js, .css, .html files** — the engine blocks this
- To APPEND code, use a patch where `old` = last few lines of file, `new` = those lines + your additions
- Read the file FIRST with `file_read` so your patch targets match exactly

### Safety Guardrails

- `ghost_supervisor.py` is protected — cannot be modified via evolve
- Rate limit: max 5 evolutions per hour
- Auto-rollback: incomplete evolutions cleaned up on next loop
- Backup stored at `~/.ghost/evolve/backups/`

## Skills System

Skills live in `skills/<name>/SKILL.md` (bundled) or `~/.ghost/skills/<name>/SKILL.md` (user).

### SKILL.md Format

```yaml
---
name: skill-name
description: "Brief description"
triggers:
  - keyword1
  - phrase two
tools:
  - tool_name1
  - tool_name2
priority: 75
---

# Markdown instructions injected into system prompt when triggered
```

- `triggers:` MUST be a flat YAML list of plain strings
- Higher `priority` = matched first when multiple skills compete
- `tools:` lists which tools the skill needs access to

## Config & State Files

| Path | Purpose |
|------|---------|
| `~/.ghost/config.json` | All settings (model, API keys, feature toggles) |
| `~/.ghost/feed.json` | Activity feed (shared daemon ↔ dashboard) |
| `~/.ghost/action.json` | Dashboard → daemon commands |
| `~/.ghost/memory.db` | SQLite FTS5 memory database |
| `~/.ghost/evolve/history.json` | Evolution history |
| `~/.ghost/evolve/backups/` | Tar.gz backups before each evolution |
| `~/.ghost/evolve/deploy_pending` | Deploy marker (triggers supervisor restart) |
| `~/.ghost/logs/tool_loop_debug.jsonl` | Debug log of all tool loop sessions |
| `~/.ghost/uploads/` | User file uploads (chat attachments) |
| `~/.ghost/paused` | Pause marker file |
| `SOUL.md` | Agent identity/personality (project root) |
| `USER.md` | User profile/preferences (project root) |

## Communication Patterns

- **Dashboard → Daemon**: writes `~/.ghost/action.json`, daemon polls it
- **Daemon → Dashboard**: writes `~/.ghost/feed.json`, dashboard API reads it
- **Chat**: Flask serves SSE stream from `ChatSession` object; daemon processes in background thread
- **Restart**: `evolve_deploy` writes marker → supervisor detects → kills Quinely → relaunches → dashboard SSE reconnects
- **Status**: `app.js` polls `/api/status` every 5 seconds — fires `ghost:restarted` when server recovers from downtime
