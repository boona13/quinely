# Ghost Configuration

Ghost stores configuration in `~/.ghost/config.json`. All settings can be changed from the dashboard with hot-reload (no restart needed), via the API, or by editing the file directly.

## Configuration Keys

### Core

| Key | Type | Default | Description |
|---|---|---|---|
| `model` | string | `"google/gemini-2.0-flash-001"` | LLM model ID (provider/model format). |
| `poll_interval` | float | `0.5` | How often the daemon checks for new inputs (seconds). |
| `min_length` | int | `30` | Minimum input length to process. Shorter inputs are ignored. |
| `rate_limit_seconds` | int | `3` | Minimum seconds between processing two inputs. Prevents rapid-fire. |
| `max_input_chars` | int | `4000` | Maximum characters accepted per input. Longer inputs are truncated. |
| `max_feed_items` | int | `50` | Number of feed entries to keep in `feed.json`. Older entries are pruned. |

### Feature Toggles

| Key | Type | Default | Description |
|---|---|---|---|
| `enable_tool_loop` | bool | `true` | Enable multi-turn tool execution. If `false`, Ghost uses single-shot LLM calls without tools. |
| `tool_loop_max_steps` | int | `40` | Maximum tool-loop iterations per task. Prevents infinite loops. |
| `enable_memory_db` | bool | `true` | Enable persistent SQLite memory. |
| `enable_plugins` | bool | `true` | Enable the plugin system. |
| `enable_skills` | bool | `true` | Enable the skill matching system. |
| `enable_system_tools` | bool | `true` | Enable core system tools (shell_exec, file_read, etc.). |
| `enable_browser_tools` | bool | `true` | Enable browser automation. Requires Playwright. |
| `enable_cron` | bool | `true` | Enable the cron scheduler for scheduled tasks. |
| `enable_evolve` | bool | `true` | Allow self-modification via the evolution engine. |
| `evolve_auto_approve` | bool | `false` | Skip approval for evolution changes (user approval required if false). |
| `enable_growth` | bool | `true` | Enable autonomous improvement routines. |
| `enable_voice` | bool | `true` | Enable Voice Wake + Talk Mode. |
| `enable_canvas` | bool | `true` | Enable the Canvas visual output panel. |
| `enable_integrations` | bool | `true` | Enable Google Workspace and third-party integrations. |
| `enable_webhooks` | bool | `true` | Enable webhook triggers for event-driven automation. |
| `enable_skill_registry` | bool | `true` | Enable the GhostHub public skill registry. |
| `enable_channels` | bool | `true` | Enable multi-channel messaging. |
| `enable_nodes` | bool | `true` | Enable GhostNodes (local AI capabilities). |

### Security

| Key | Type | Default | Description |
|---|---|---|---|
| `allowed_commands` | list | *(see below)* | Whitelist of shell commands the LLM can execute via `shell_exec`. Commands not in this list are rejected. |
| `allowed_roots` | list | `["/Users/<you>"]` | Directory whitelist for file operations. `file_read`, `file_write`, and `file_search` are restricted to paths under these roots. |

**Default `allowed_commands`:**

```json
[
  "ls", "pwd", "cd", "echo", "date", "cat", "head", "tail", "wc",
  "grep", "find", "which", "whoami", "hostname", "uname",
  "df", "du", "uptime", "env",
  "mv", "cp", "mkdir", "rm", "rmdir", "touch", "chmod", "chown",
  "ln", "stat", "file", "tree",
  "sort", "uniq", "awk", "sed", "tr", "cut", "diff", "patch",
  "xargs", "tee",
  "zip", "unzip", "tar", "gzip", "bzip2", "xz",
  "python3", "python", "node", "npm", "npx", "pip", "pip3",
  "git", "make", "cmake",
  "curl", "wget", "ssh", "scp", "rsync", "ping", "dig",
  "ps", "kill", "sleep",
  "md5", "shasum", "base64", "openssl",
  "sqlite3", "jq", "rg", "fd", "bat", "exa", "eza",
  "open", "pbcopy", "pbpaste", "say", "defaults", "sw_vers"
]
```

### Dashboard

| Key | Type | Default | Description |
|---|---|---|---|
| `dashboard_port` | int | `3333` | Port for the web dashboard. If the port is busy, Ghost tries the next 9 ports. |

### Skill Management

| Key | Type | Default | Description |
|---|---|---|---|
| `disabled_skills` | list | `[]` | List of skill names to exclude from matching. Managed via the dashboard's Skills page. |
| `skill_model_overrides` | dict | `{}` | Per-skill model overrides. Keys are skill names, values are model IDs. |

### Coding Model Dispatcher

| Key | Type | Default | Description |
|---|---|---|---|
| `coding_model_budget` | string/number | `"auto"` | Budget for coding tasks. Presets: `"free"` (disables self-evolution), `"low"` (≤$0.50/MTok), `"medium"` (≤$2/MTok), `"high"` (≤$6/MTok), `"auto"` (best value ratio). A raw number like `1.5` is treated as max $/MTok. |
| `coding_model_override` | string/null | `null` | Force a specific model for coding tasks (e.g. `"anthropic/claude-opus-4.6"`). Bypasses the dispatcher entirely. |
| `min_swe_bench_score` | float | `78.0` | Minimum acceptable SWE-bench Verified score for coding model selection. Auto-relaxed with a warning if no model qualifies within budget. |
| `coding_jobs` | list | `["_quinely_growth_feature_implementer", "_quinely_growth_bug_hunter"]` | Cron job names that should use the coding model instead of the default. (Legacy `_ghost_growth_*` names are auto-migrated on load.) |

The dispatcher checks all configured providers and picks the cheapest route to the highest-quality model. For example, a user with a ChatGPT Plus subscription gets `gpt-5.3-codex` at $0 through OAuth. Setting budget to `"free"` completely disables coding cron jobs to avoid low-quality output.

### Growth & Autonomy

| Key | Type | Default | Description |
|---|---|---|---|
| `growth_schedules` | dict | *(defaults)* | Interval overrides for each autonomous growth routine. Keys are routine names, values are cron expressions or intervals. |
| `max_evolutions_per_hour` | int | `3` | Rate limit for self-modification. |

### Voice

| Key | Type | Default | Description |
|---|---|---|---|
| `voice_wake_word` | string | `"ghost"` | Wake word for Voice Wake mode. |
| `voice_stt_provider` | string | `"moonshine"` | Speech-to-text provider (moonshine, openai, groq, vosk). |
| `voice_tts_provider` | string | `"edge"` | Text-to-speech provider (edge, openai, elevenlabs). |
| `voice_tts_voice` | string | `"en-US-AriaNeural"` | TTS voice ID. |
| `voice_sensitivity` | float | `0.5` | Wake word detection sensitivity (0.0-1.0). |

### Channels

| Key | Type | Default | Description |
|---|---|---|---|
| `channels` | dict | `{}` | Per-channel configuration. Managed via the Channels dashboard page. |
| `default_alert_channels` | list | `[]` | Channels that receive system alerts and notifications. |

## Environment Variables

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter API key. Overrides `api_key` in config. |
| `OPENAI_API_KEY` | OpenAI direct API key. |
| `ANTHROPIC_API_KEY` | Anthropic direct API key. |
| `GOOGLE_API_KEY` | Google Gemini API key. |
| `DEEPSEEK_API_KEY` | DeepSeek API key. |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS API key. |

## File Locations

| Path | Description |
|---|---|
| `~/.ghost/config.json` | Main configuration file |
| `~/.ghost/auth_profiles.json` | Provider API keys and OAuth tokens |
| `~/.ghost/log.json` | Action history (last 500 entries) |
| `~/.ghost/feed.json` | Activity feed (last 50 entries) |
| `~/.ghost/ghost.pid` | Running daemon PID |
| `~/.ghost/memory.db` | SQLite persistent memory database |
| `~/.ghost/future_features.json` | Evolution backlog |
| `~/.ghost/action_items.json` | User action items |
| `~/.ghost/growth_log.json` | Autonomous growth history |
| `~/.ghost/integrations.json` | Google OAuth tokens |
| `~/.ghost/channels.json` | Channel configurations |
| `~/.ghost/coding_benchmarks.json` | SWE-bench scores for coding model selection (auto-seeded, weekly auto-updated) |
| `~/.ghost/model_dispatch_cache.json` | Cached coding model selection (24h TTL) |
| `~/.ghost/cron/jobs.json` | Cron job definitions |
| `~/.ghost/evolve/backups/` | Project backups before self-modifications |
| `~/.ghost/audio/` | Generated TTS audio files |
| `~/.ghost/voice/` | Voice capture and STT models |
| `~/.ghost/canvas/` | Canvas session files |
| `~/.ghost/generated_images/` | AI-generated images |
| `~/.ghost/memory/sessions/` | Session summaries |
| `~/.ghost/skill_registry/` | GhostHub registry cache |
| `~/.ghost/skills/` | User-created and registry-installed skills |
| `~/.ghost/plugins/` | User plugins |
| `~/.ghost/screenshots/` | Processed screenshots |
| `~/.ghost/state_backups/` | State file backups from repair |
| `<project>/SOUL.md` | Agent personality definition |
| `<project>/USER.md` | User profile for personalization |

## Default Configuration

The full default configuration:

```json
{
  "model": "google/gemini-2.0-flash-001",
  "poll_interval": 0.5,
  "min_length": 30,
  "rate_limit_seconds": 3,
  "max_input_chars": 4000,
  "max_feed_items": 50,
  "enable_tool_loop": true,
  "tool_loop_max_steps": 40,
  "enable_memory_db": true,
  "enable_plugins": true,
  "enable_skills": true,
  "enable_system_tools": true,
  "enable_browser_tools": true,
  "enable_cron": true,
  "enable_evolve": true,
  "evolve_auto_approve": false,
  "enable_growth": true,
  "enable_voice": true,
  "enable_canvas": true,
  "enable_integrations": true,
  "enable_webhooks": true,
  "enable_skill_registry": true,
  "enable_channels": true,
  "enable_nodes": true,
  "allowed_commands": [
    "ls", "pwd", "cd", "echo", "date", "cat", "head", "tail", "wc",
    "grep", "find", "which", "whoami", "hostname", "uname",
    "df", "du", "uptime", "env",
    "mv", "cp", "mkdir", "rm", "rmdir", "touch", "chmod", "chown",
    "ln", "stat", "file", "tree",
    "sort", "uniq", "awk", "sed", "tr", "cut", "diff", "patch",
    "xargs", "tee",
    "zip", "unzip", "tar", "gzip", "bzip2", "xz",
    "python3", "python", "node", "npm", "npx", "pip", "pip3",
    "git", "make", "cmake",
    "curl", "wget", "ssh", "scp", "rsync", "ping", "dig",
    "ps", "kill", "sleep",
    "md5", "shasum", "base64", "openssl",
    "sqlite3", "jq", "rg", "fd", "bat", "exa", "eza",
    "open", "pbcopy", "pbpaste", "say", "defaults", "sw_vers"
  ],
  "allowed_roots": ["/Users/<your-username>"]
}
```

## CLI Configuration

Settings can also be passed as CLI arguments (these override config file values for that session):

```bash
python ghost.py --api-key sk-or-v1-...    # Override API key
python ghost.py --model anthropic/claude-3.5-sonnet  # Override model
python ghost.py --poll 1.0                 # Override poll interval
```

## Resetting Configuration

### Via Dashboard

Go to Configuration page, click "Reset to Defaults".

### Via File

```bash
rm ~/.ghost/config.json
bash start.sh   # Recreates with defaults
```

### Via API

```bash
curl -X PUT http://localhost:3333/api/config \
  -H "Content-Type: application/json" \
  -d '{"model":"google/gemini-2.0-flash-001","poll_interval":0.5}'
```
