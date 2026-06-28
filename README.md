<p align="center">
  <img src="ghost_logo3.png" alt="Ghost" width="180">
</p>

<h1 align="center">Ghost</h1>

<p align="center"><strong>The AI agent that rewrites its own source code, reviews its own PRs, and deploys itself — while you sleep.</strong></p>

Ghost is an autonomous, self-evolving AI agent that runs locally on your machine. It doesn't just respond to prompts — it operates 24/7 with 260+ tools, 22 AI nodes, and 14 autonomous growth routines that continuously improve its own codebase. It writes code, submits internal PRs, runs an adversarial code review with a separate LLM instance, deploys approved changes, and rolls back if anything breaks. If it crashes, it reads the traceback, diagnoses the root cause, patches itself, and restarts.

It's a full operating system for AI autonomy — not a chatbot.

> **One agent. 260+ tools. 22 AI nodes. 7 LLM providers. MCP client. 3 messaging channels. Zero cloud dependencies.**

---

## Why Ghost

Most AI tools wait for instructions. Ghost operates autonomously.

- **Self-evolution** — Ghost modifies its own codebase through a complete CI/CD pipeline: plan → apply → test → adversarial PR review → deploy → rollback. Every change is backed up, tested, reviewed by a separate LLM, and auto-rolled back if it fails.
- **Self-healing** — If Ghost crashes, it reads the crash report, diagnoses the root cause from the traceback, writes a fix, tests it, and restarts. After 5 consecutive crashes, the supervisor auto-rolls back to the last known good state.
- **Adversarial PR review** — Every self-modification goes through a separate LLM instance acting as a strict senior code reviewer. The reviewer searches the codebase with ripgrep, leaves inline comments, suggests exact code replacements, and submits APPROVE / REQUEST_CHANGES / BLOCK verdicts. Rejected PRs preserve the branch and all feedback for fix-and-resubmit — up to 5 rounds.
- **260+ built-in tools** — File system, precise editing, git, shell, browser automation, web search, web fetch, vision, image generation, TTS, voice, Google Workspace, messaging channels, cron, credentials, code intelligence, data extraction, security audits, MCP-bridged servers, and more.
- **22 AI nodes** — Local image generation (Stable Diffusion / FLUX), video generation (Kling, Runway, Minimax), audio (TTS, STT, music, voice cloning), vision (captioning, OCR, depth estimation), with an intelligent GPU load balancer and pipeline engine to chain them together.
- **14 autonomous routines** — Tech scouting, bug hunting, security patrols, competitive intelligence, skill improvement, soul evolution, health checks, and more — all on configurable cron schedules.
- **Multi-channel** — Talk to Ghost on Telegram, Discord, and WhatsApp. Every channel gets message queuing, crash recovery, streaming, and security policies.
- **Local-first** — Runs on your machine. Your data stays on your machine. No cloud subscription. No telemetry.

---

## Quick Start

### Prerequisites

- Python 3.10+
- An API key from any supported provider (or none — Ghost starts a setup wizard)

### One-liner (macOS / Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/boona13/ghost/main/install.sh | bash
```

This clones the repo, creates a virtual environment, installs dependencies, starts Ghost, and opens the dashboard in your browser.

```bash
# Non-interactive with Playwright and API key
curl -fsSL https://raw.githubusercontent.com/boona13/ghost/main/install.sh | bash -s -- --with-playwright --api-key sk-or-v1-...

# Skip all prompts
curl -fsSL https://raw.githubusercontent.com/boona13/ghost/main/install.sh | bash -s -- --no-interactive

# Fresh install — wipe existing ~/.ghost/ data and start clean (creates a backup first)
curl -fsSL https://raw.githubusercontent.com/boona13/ghost/main/install.sh | bash -s -- --fresh
```

Or clone first, then install:

```bash
git clone https://github.com/boona13/ghost.git
cd ghost
bash install.sh
```

### Install (Windows)

```powershell
git clone https://github.com/boona13/ghost.git
cd ghost
powershell -ExecutionPolicy Bypass -File install.ps1
```

```bat
start.bat
```

### Start / Stop

| | macOS / Linux | Windows |
|---|---|---|
| **Start** | `./start.sh` | `start.bat` |
| **Stop** | `./stop.sh` | `stop.bat` |

Open [http://localhost:3333](http://localhost:3333) — the setup wizard guides you through connecting your AI providers.

### Manual Install

```bash
git clone https://github.com/boona13/ghost.git
cd ghost
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt

# Optional: browser automation
pip install playwright && python -m playwright install chromium

python ghost_supervisor.py    # With supervisor (recommended)
# or
python ghost.py               # Standalone
```

### Docker

```bash
docker build -f Dockerfile.test -t ghost .
docker run -d --name ghost -p 3333:3333 ghost
```

### No API Key?

Ghost starts without one. Open [http://localhost:3333](http://localhost:3333) and the setup wizard walks you through provider selection, API key entry, connection testing, and fallback chain configuration — all from the browser.

---

## The Tool System — 260+ Tools

Ghost's tool system is a multi-turn execution engine. The LLM calls tools, gets results, decides what to do next, and loops — up to 200 steps per session with automatic context compaction, loop detection, and deferral prevention.

### Core Tools

| Category | Tools | Highlights |
|---|---|---|
| **File System** | `shell_exec`, `file_read`, `file_write`, `edit_file`, `apply_patch`, `file_search`, `grep`, `glob` | Whitelisted commands, workspace scoping, codebase write protection |
| **Precise Editing** | `edit_file` (exact unique search/replace), `apply_patch` (context-located unified diffs) | Surgical edits without rewriting whole files — uniqueness checks, fuzzy hunk location, diff previews |
| **Version Control** | `git_status`, `git_diff`, `git_log`, `git_add`, `git_commit`, `git_branch`, `git_init` | Structured git on user project repos — refuses to touch Ghost's own evolve-managed repo |
| **Browser** | `browser` (navigate, click, type, screenshot, PDF, tabs) | Accessibility tree targeting — no fragile CSS selectors |
| **Web Intelligence** | `web_search` (6 providers), `web_fetch` (5-tier extraction) | Perplexity, Grok, Brave, Gemini, OpenAI; Readability → Firecrawl pipeline |
| **Memory** | `memory_save/search`, `semantic_memory`, `hybrid_memory` | SQLite + FTS5 + vector store; **automatic pre-turn retrieval** with fused reranking (keyword + semantic + recency) |
| **Vision** | `image_analyze`, `screenshot_analyze` | 5-provider fallback: OpenAI, OpenRouter, Gemini, Anthropic, Ollama |
| **Image Gen** | `generate_image` | OpenRouter (Gemini 3 Pro), Google Gemini, OpenAI DALL-E / gpt-image-1 |
| **Voice** | `voice_wake_start/stop`, `voice_talk_start/stop` | Always-on wake word detection, continuous conversation, 5 STT providers |
| **TTS** | `text_to_speech`, `tts_voices` | Edge TTS (free), OpenAI, ElevenLabs |
| **Canvas** | `canvas` | Live HTML/CSS/JS visual output panel with JS bridge injection |
| **Google Workspace** | Gmail, Calendar, Drive, Docs, Sheets | Full OAuth 2.0 — read, write, search, share, manage |
| **Channels** | Messaging tools | Send, broadcast, react, edit, unsend, poll, thread context, security |
| **Cron** | `cron_add/list/remove/run` | Standard cron, fixed intervals, one-shot, and event-driven scheduling |
| **Credentials** | `credential_save/get/list` | Structured credential storage with audit logging |
| **Data Extraction** | `smart_extract`, `extract_table` | 15+ pattern types: emails, phones, prices, credit cards, UUIDs, tables |
| **Code Intelligence** | `analyze_code_file`, `analyze_repository`, `find_code_patterns` | LOC, cyclomatic complexity, maintainability index, bug pattern detection |
| **Webhooks** | `webhook_create/list/delete/test` | Event-driven automation with HMAC verification and template injection |
| **Diagnostics** | `doctor_run/fix`, `security_audit/fix`, `repair_state` | Health checks, security audits, state repair, dependency doctor |
| **MCP** | `mcp_status` + bridged `mcp_<server>_<tool>` | Connects to external Model Context Protocol servers and exposes their tools natively |

### Self-Evolution Tools

| Tool | What It Does |
|---|---|
| `evolve_plan` | Creates backup + git branch, classifies modification level (1-5) |
| `evolve_apply` | Applies code changes with patch-only enforcement and fuzzy matching |
| `evolve_test` | Three-stage validation: syntax → import → smoke test (`--dry-run`) |
| `evolve_submit_pr` | Submits internal PR for adversarial review by a separate LLM |
| `evolve_deploy` | Waits for running jobs to finish, then triggers supervisor restart |
| `evolve_rollback` | Selective restore from backup (changed files only) |
| `evolve_resume` | Resumes an interrupted evolution (preserves branch + reviewer feedback) |

### Future Features & Autonomy Tools

| Tool | What It Does |
|---|---|
| `add_future_feature` | Queue a feature with P0–P3 priority for autonomous implementation |
| `start_future_feature` | Begin implementing — picks up context from previous attempts |
| `complete_future_feature` | Mark done after successful deploy |
| `add_action_item` | Flag something that needs user attention |
| `log_growth_activity` | Record autonomous improvement activity |

### Dynamic Tool System

Ghost can extend its own toolset at runtime:

- **`tools_create`** — Create new tools from scratch (writes TOOL.yaml + tool.py)
- **`tools_install_github`** — Install tools directly from GitHub repos
- **`tools_validate`** — Validate tool syntax and registration
- **Event hooks** — `on_boot`, `on_tool_call`, `on_evolve_complete`, `on_media_generated`, and more

### Tool Loop Intelligence

- **200-step sessions** with adaptive context management
- **Context compaction** at ~80K tokens — LLM-assisted summarization preserves imports, signatures, and decisions
- **6 loop detectors** — generic repeat, poll no-progress, ping-pong, tool saturation, global circuit breaker, warning accumulation
- **Deferral prevention** — pushes back when the LLM tries to give up early
- **Incomplete workflow enforcement** — blocks `task_complete` if an evolution workflow is unfinished

---

## GhostNodes — 22 AI Capabilities

GhostNodes is a modular AI capability system. Each node is a self-contained plugin (just a `NODE.yaml` manifest + `node.py` entry point) that registers tools with Ghost. Nodes can run locally on your GPU or call cloud APIs.

### Bundled Nodes

| Category | Nodes | What They Do |
|---|---|---|
| **Image Generation** | `stable-diffusion`, `background-remove`, `image-upscale`, `image-inpaint`, `style-transfer` | FLUX.2/FLUX.1/SDXL text-to-image, background removal (U2-Net), 2–4x upscaling (Real-ESRGAN), inpainting, style transfer |
| **Video** | `kling-video`, `runway-video`, `minimax-video`, `runware-video`, `video-gen`, `video-router`, `video-composer`, `image-to-video` | Text-to-video, image-to-video, multi-provider routing, clip stitching with audio overlay and transitions |
| **Audio** | `bark-tts`, `whisper-stt`, `music-gen`, `voice-clone`, `voice-fx`, `sound-effects` | Expressive TTS (13+ languages), 99-language STT, music generation, voice cloning, audio effects, sound FX |
| **Vision** | `florence-vision`, `surya-ocr`, `depth-estimation` | Image captioning, object detection, OCR for 90+ languages, depth maps |

### GPU Resource Manager

Ghost manages GPU memory like a production ML serving framework:

- **Smart model eviction** — Composite scoring: `(use_frequency × recency) / (load_cost × vram_weight)` instead of naive LRU
- **Serialized loading** — Semaphore-based gate prevents OOM from concurrent model loads
- **Auto-detection** — NVIDIA CUDA, Apple Silicon MPS/MLX, CPU fallback
- **Persistent statistics** — Load count, frequency, average load time per model across restarts
- **5-minute watchdog** — Auto-releases stuck load gates

### Pipeline Engine

Chain any node tools into multi-step workflows:

```
text_to_image → remove_background → upscale_image (4x) → image_to_video → add_music
```

The engine validates references, executes sequentially, and automatically routes outputs between steps. Supports async execution, cancellation, and state persistence.

### Node Development

- **Two files to create a node** — `NODE.yaml` + `node.py` with `register(api)`
- **Rich SDK** — GPU allocation, model downloads, media storage, cloud API keys, persistent data
- **Security scanning** — AST parsing, dangerous code pattern detection before installation
- **GhostNodes Registry** — Community marketplace for discovering and installing nodes

---

## Self-Evolution Engine

Ghost modifies its own source code through a controlled pipeline with adversarial review.

### The Pipeline

```
evolve_plan → evolve_apply (1-5x) → evolve_test → evolve_submit_pr → PR review → deploy/rollback
```

1. **Plan** — Creates a full project backup (`.tar.gz`), a git feature branch (`evolve/<id>`), and classifies the modification level (1 for skills, 5 for core files, 99 for protected)
2. **Apply** — Writes changes with safety guards: protected files can't be touched, existing files must use search/replace patches (no full rewrites), 30KB max for new files, fuzzy patch matching with whitespace normalization
3. **Test** — Three-stage validation: `ast.parse` every changed file → attempt imports → `ghost.py --dry-run` smoke test
4. **Submit PR** — Creates an internal PR with full diff, runs semantic lint to catch anti-patterns before review
5. **Review** — A separate LLM instance with 7 tools performs adversarial code review (see below)
6. **Deploy** — Waits for running cron jobs to finish, writes a deploy marker, supervisor handles restart
7. **Rollback** — Selective restore (only changed files) or full restore for crash recovery

### Adversarial PR Review

Every self-modification goes through a **separate LLM instance** acting as a strict senior code reviewer. The reviewer gets 7 specialized tools:

| Tool | What It Does |
|---|---|
| `read_pr_diff` | Browse diffs per-file (sorted: new files → integration files → patches) |
| `read_pr_file` | Read full file content with line numbers |
| `grep_codebase` | Search the codebase with ripgrep to verify wiring and find duplicates |
| `leave_comment` | Leave inline comments with severity (critical/high/warning/suggestion) |
| `suggest_change` | Propose exact code replacements |
| `get_my_comments` | Review own comments before submitting verdict |
| `submit_review` | Final verdict: APPROVE, REQUEST_CHANGES, or BLOCK |

The reviewer checks ~15 categories: code quality, security, frontend-backend integration, tool registration wiring, interface compatibility, thread safety, Python correctness, duplicate functionality, cross-platform compatibility, and more.

**Fix-and-resubmit** — When a PR is rejected, the branch is preserved with all reviewer feedback. The next attempt uses `evolve_resume()` to pick up exactly where it left off, reading all comments and applying targeted patches. Up to 5 rounds of review/revision.

### Safety Guarantees

- Protected files (`ghost_supervisor.py`) can never be self-modified
- Protected patterns (`PROTECTED_FILES`, `CORE_COMMANDS`) can never be removed
- Max 25 evolutions per hour
- Approval required for level 3+ changes unless `evolve_auto_approve` is set
- Supervisor auto-rolls back after 5 consecutive crashes

---

## Autonomous Growth — 14 Routines

Ghost improves itself on configurable schedules. Each routine is a specialized autonomous agent with a detailed system prompt, full tool access, and its own schedule.

| Routine | Schedule | What It Does |
|---|---|---|
| **Tech Scout** | Every 12h | Browses AI/tech news, discovers new APIs and tools, queues features for implementation |
| **Health Check** | Every 2h | Tests APIs, disk, memory DB, and **self-tests every dashboard endpoint** to find silent 500 errors |
| **Bug Hunter** | Every 6h | Scans `~/.ghost/log.json` for error patterns, diagnoses root causes, queues P1 fixes |
| **Security Patrol** | Daily 5am | Runs security audits on permissions, credentials, shell allowlists, config hardening |
| **Competitive Intel** | Mon/Thu 6am | Studies competing AI products, identifies specific features Ghost is missing, queues implementations |
| **Skill Improver** | Daily 3am | Reviews and upgrades skill definitions, trigger matching, and instructions |
| **Soul Evolver** | Weekly Sun | Reads SOUL.md + growth logs + user interactions, proposes personality updates |
| **User Context Sync** | Every 4h | Reads Gmail/Calendar to learn patterns and anticipate needs |
| **Content Health** | Weekly Sun | Tests web extraction pipeline quality across diverse URL types |
| **Visual Monitor** | Every 8h | Screenshot analysis for visual issues and accessibility |
| **Model Benchmarks** | Weekly Sun | Searches for the latest SWE-bench leaderboard, updates coding model benchmark data |
| **Goal Executor** | Every 30m | Runs the deterministic Goal Engine — plans pending goals, executes ALL steps back-to-back in one pass, verifies each step, quality-checks output, then completes the goal |
| **Feature Implementer** | Event-driven | Picks features from the priority queue, implements them through the full evolution pipeline |
| **Implementation Auditor** | Event-driven | Verifies deployed features across 4 layers: structural wiring, API contracts, frontend-backend integration, actual rendering |

### Self-Healing

```
Crash → Supervisor captures traceback → Writes crash report → Exponential backoff restart
→ After 5 crashes: auto-rollback to last backup
→ On next boot: reads crash report → diagnoses root cause → writes fix → tests → deploys → restarts
```

Ghost even detects when a crash was caused by importing a file it intentionally deleted, and removes the import instead of recreating the file.

### Future Features Queue

Ghost's autonomous product management system. Features flow in from Tech Scout, Bug Hunter, Competitive Intel, Security Patrol, Health Check, and user requests.

- **P0–P3 priority** — P0 requires user approval; P1 triggers immediate implementation via `fire_now()`
- **Smart deduplication** — Exact title match + 70% fuzzy word overlap catches near-duplicates
- **Dependency ordering** — Features with dependencies wait until prerequisites are implemented
- **Stale recovery** — In-progress features from crashed sessions are auto-recovered on startup
- **Cooldown gating** — Rejected features wait 15 minutes before retry
- **Auto-deferral** — 3 failures → feature is deferred

---

## Coding Model Dispatcher — Budget-Aware Model Selection

Ghost doesn't use the same model for everything. Health checks run on the cheap default model. But when Ghost is rewriting its own code — feature implementation, bug fixing — it automatically switches to the **best coding model the user can afford**.

The dispatcher scores models by **SWE-bench Verified** (the industry standard for real-world bug fixing), filters by the user's budget, and finds the **cheapest route** across all configured providers.

| Budget | What You Get | Cost |
|---|---|---|
| `auto` (default) | Best value: highest SWE-bench / cost ratio | Varies |
| `free` | Self-evolution disabled entirely | $0 |
| `low` | MiniMax M2.5 (80.2% SWE-bench) | ≤$0.50/MTok |
| `medium` | GPT-5.2 (80.0%) or MiniMax M2.5 | ≤$2/MTok |
| `high` | Claude Opus 4.6 (80.8% — best) | ≤$6/MTok |

**Multi-provider routing** — The dispatcher checks all 7 providers and picks the cheapest path. A user with a ChatGPT Plus subscription gets GPT-5.3-Codex at $0 through OAuth. A user with an Anthropic key gets Claude Opus at $3/MTok instead of $5 through OpenRouter.

**Self-updating benchmarks** — A weekly cron job searches for the latest SWE-bench leaderboard and updates the benchmark data automatically. When a new model drops that's better, Ghost discovers and adopts it.

Configure from the dashboard (Models → Coding Model Dispatcher) or via config:

```json
{
  "coding_model_budget": "auto",
  "coding_model_override": null,
  "min_swe_bench_score": 78.0
}
```

---

## 7 LLM Providers with Automatic Fallback

Ghost supports **OpenRouter** (200+ models), **OpenAI** (direct API), **OpenAI Codex** (ChatGPT subscription via OAuth — no extra cost), **Anthropic** (Claude), **Google Gemini** (free tier available), **DeepSeek**, and **Ollama** (local, completely free).

Configure one or all — Ghost automatically falls back through your provider chain with jittered exponential backoff, escalating cooldowns (60s → 5m → 25m → 1h), and periodic probing of failed providers for recovery detection.

---

## Messaging Channels

| Channel | How It Works |
|---|---|
| **Telegram** | Bot API with reactions, threading, streaming, and polls |
| **Discord** | Webhook (zero-dependency) or full bot mode via discord.py |
| **WhatsApp** | QR code linking via neonize (best-effort) or Business API via webhook |

Every channel gets: message queuing with write-ahead logging, exponential backoff retries, crash recovery, per-channel formatting, real-time streaming, DM security policies, rate limiting, health monitoring, and per-channel onboarding wizards. Telegram and Discord are the most battle-tested; WhatsApp via neonize depends on an unofficial library and may require extra setup. Each channel is independently enabled — the startup banner shows how many of the configured channels are currently active.

---

## 47 Skills + GhostHub Registry

Specialized knowledge injected automatically when relevant:

| Category | Skills |
|---|---|
| **Productivity** | Apple Notes, Apple Reminders, Notion, Trello, Things (Mac) |
| **Development** | GitHub, code reviewer, fullstack development, UI development, browser automation |
| **Research** | Deep researcher (multi-source, structured output, credibility scoring), news search, blog watcher, competitive intelligence |
| **Content** | Content creator, social content, email drafting, translation, summarization |
| **Media** | Spotify player, GIF search, video frame extraction, image generation |
| **Social** | X/Twitter growth (post, like, comment, repost, follow with deduplication tracking) |
| **Finance** | Trading analysis (chart patterns, technical indicators, portfolio tracking) |
| **System** | Ghost system management, webhooks, weather, tmux, 1password, PDF tools, speech-to-text |

**GhostHub** — A public skill registry where anyone can publish and install skills with one click from the dashboard. Skills are security-scanned before installation with 34+ regex patterns across 5 threat categories (prompt injection, data exfiltration, destructive commands, obfuscation, self-modification).

---

## Additional Systems

### Neural Embeddings & Semantic Memory

Semantic recall is powered by a real local neural embedding model, not just keyword overlap.

- **Local neural embeddings** — `ghost_embeddings.py` uses [`model2vec`](https://github.com/MinishLab/model2vec) static embeddings (default `minishlab/potion-base-8M`, 256-dim). Pure-Python + numpy, CPU-only, no torch, no API key. The ~30 MB model downloads once and is cached. This finds conceptually related memories even with no shared words (e.g. *"which language do I like?"* → *"my favorite is Rust"*).
- **Graceful fallback** — if the model can't be loaded (offline, disabled, or missing dep) the embedder transparently falls back to the legacy 128-dim hash embedding, so memory never breaks.
- **ANN index** — searches run as vectorized NumPy cosine over an in-memory matrix cache (sub-millisecond for thousands of vectors), auto-invalidated on writes. `hnswlib` is used automatically if present, but is never required.
- **Mixed vector spaces** — every row records its embedding `model`, so legacy hash vectors and neural vectors coexist; queries are scored in the correct space and legacy rows can be re-embedded with `reembed_stale()`.
- **Auto-indexing** — on boot a background thread embeds existing long-term memories (`memory.db`) into the semantic store so retrieval has a populated vector space from the first turn. Idempotent and non-blocking. Toggle with `enable_neural_embeddings`; choose the model with `embedding_model`.

### Automatic Memory Retrieval (RAG)

Ghost doesn't wait for the model to decide to look something up. Before every chat turn it automatically retrieves and injects the most relevant long-term memories.

- **Fused retrieval** — combines keyword/full-text recall (SQLite FTS) with neural semantic vector similarity (see above) over Ghost's existing memory stores (no schema changes).
- **Reranker** — blends each candidate's source score with query-term overlap and a recency boost, dedupes near-identical memories, and trims to a token budget.
- **Safe by default** — greetings and very short messages are skipped, every backend is wrapped defensively, and any retrieval failure yields no injection rather than breaking the turn. Toggle with `enable_auto_retrieval`.

### MCP — Model Context Protocol

Ghost is an MCP **client**: it connects to external MCP tool servers (filesystem, Linear, Sentry, Stripe, Playwright, or any custom server) and bridges their tools straight into the registry, where the LLM calls them like native tools.

- **stdio transport** — newline-delimited JSON-RPC 2.0 over each server's stdin/stdout. Pure stdlib, cross-platform, no extra dependencies.
- **Full handshake** — `initialize` → `notifications/initialized` → `tools/list`, then live `tools/call` invocation with input-schema passthrough.
- **Namespaced bridging** — each remote tool registers as `mcp_<server>_<tool>` so there are no collisions with native tools.
- **Config** — drop a Claude/Cursor-compatible `~/.ghost/mcp_servers.json` (`mcpServers` map with `command` / `args` / `env`, optional `"disabled": true`). See `~/.ghost/mcp_servers.example.json`. Use `mcp_status` to inspect connected servers and tools.

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allow"]
    }
  }
}
```

### Browser Automation

Playwright-based browser control with accessibility tree snapshots, ref-based element targeting, and full action support: navigate, click, type, fill forms, scroll, hover, upload files, execute JavaScript, take screenshots, export PDFs, and manage tabs.

### Web Intelligence

**Web search** across 6 providers with automatic fallback: Perplexity (via OpenRouter or direct), Grok/xAI, OpenAI, Brave Search, and Gemini with Google Search grounding. Results cached for 15 minutes.

**Web fetching** with a 5-tier extraction pipeline: Cloudflare Markdown → Mozilla Readability → Smart BeautifulSoup → Firecrawl API → Regex fallback. Includes SSRF protection, prompt injection defense, and HTML sanitization.

### Voice Interface

- **Voice Wake** — Always-on wake word detection. Say "ghost" followed by a command. Ghost transcribes, processes, and speaks the response.
- **Talk Mode** — Continuous conversation with no wake word needed.
- **5 STT providers** — Moonshine (on-device, free), OpenRouter, OpenAI Whisper, Groq Whisper, Vosk (offline)

### Canvas

Visual output panel for rich HTML/CSS/JS content. Ghost can build interactive demos, visualizations, dashboards, and mini-apps that render alongside the chat with live reload and JS injection.

### Mid-Generation Control

Cancel or inject prompts into an active LLM generation in real-time. The interrupt system supports full state machine tracking (idle → connecting → streaming → complete/cancelled) with accumulated chunk inspection.

### X/Twitter Deduplication

SQLite-backed interaction tracker that prevents duplicate social media actions. Before any like/retweet/follow/comment, Ghost checks if the action was already performed — preventing double-likes and bot-like behavior.

### Webhook Triggers

Event-driven automation via HTTP POST. Built-in templates for GitHub (Push, PR, Issue), with custom trigger support using `{field}` placeholders. Bearer token auth, optional per-trigger HMAC verification, cooldowns, and concurrency limits.

### Execution Sandbox

Agent shell commands run through a cross-platform sandbox layer (`ghost_sandbox.py`) that uses only the standard library — no Docker, no system packages required:

- **Resource limits (POSIX)** — `setrlimit` in the child process caps CPU time (`cpu_seconds`), file size (`file_size_mb`), and core dumps; optional address-space (`memory_mb`), process (`max_processes`), and open-file caps. Applied to `shell_exec`, `shell_session`, and background processes.
- **Process-group kill on timeout** — commands start in their own session/process-group, so a wall-clock timeout (or a session/background kill) terminates the *entire* process tree — no orphaned children outliving the timeout.
- **Secret environment scrubbing** — by default (`env_mode: scrub_secrets`) variables whose names look like secrets (API keys, tokens, passwords, provider creds) are stripped from the subprocess environment, so executed commands can't read the daemon's secrets. Modes: `full`, `scrub_secrets`, `minimal`.
- **Optional OS-level isolation** — on Linux, if `bubblewrap` (`bwrap`) is present and `sandbox.isolation` is `auto`/`bwrap`, commands run in a mount namespace (read-only system, writable cwd/tmp, optional `--unshare-net` when `network: deny`). Auto-detected, never required.
- **Graceful degradation** — on Windows `resource` is unavailable, so rlimits are skipped while timeout, process-group kill, and env scrubbing still apply.

All limits are configurable under the `sandbox` config key. Status is exposed at `/api/status` and shown as a "sandboxed" badge on the dashboard.

---

## Dashboard

The web dashboard at [http://localhost:3333](http://localhost:3333) provides full management with 28+ pages:

| Page | What It Does |
|---|---|
| **Chat** | Real-time messaging with file attachments, audio transcription, tool step streaming, inline evolution approvals, voice toggle, and Canvas panel |
| **Overview** | Live daemon status, PID, uptime, action counts, feature toggles, platform info |
| **Activity Feed** | Live feed of all actions with type filtering and auto-refresh |
| **Console** | Real-time SSE event stream with category filters, search, and pause/resume |
| **Nodes** | GPU status, 22 AI capabilities, dynamic form generation from JSON schemas, inline media previews (images/audio/video/3D), pipeline management, drag-and-drop install |
| **Soul** | Edit Ghost's personality (SOUL.md) |
| **User Profile** | Edit user info (USER.md) |
| **Memory** | Search, browse, and prune the memory database |
| **Models** | Multi-provider management, fallback chain visualization, model browser with pricing, coding model dispatcher with budget control and SWE-bench leaderboard |
| **Skills** | Browse, search, enable/disable 47 skills + GhostHub Registry with security scanning |
| **Autonomy** | Action items, growth routine status, growth log, crash reports |
| **Evolution** | Self-modification history, approve/reject pending changes, view diffs, rollback |
| **Future Features** | Prioritized backlog for autonomous implementation — add, approve, reject, track |
| **Goals** | Create and monitor persistent long-horizon goals — recurring digests, research tasks, weekly reports — with real-time step progress, output history, and per-run deliverables |
| **Channels** | Configure, enable/disable, test, and monitor messaging channels (Telegram, Discord, WhatsApp) |
| **Integrations** | Google OAuth, Grok, ElevenLabs, web search providers, image gen, vision, TTS |
| **Configuration** | All settings with hot-reload — feature toggles, rate limits, growth schedules, security, voice, factory reset |
| **Cron Jobs** | Create and manage scheduled tasks |
| **Security** | AI-driven security audits with real-time streaming and auto-fix |
| **Setup** | Multi-provider wizard with connection testing and Setup Doctor |

---

## Architecture

```
ghost.py                    Main daemon — LLM routing, 260+ tool registration, GhostDaemon class
ghost_loop.py               ToolLoopEngine — multi-turn LLM + tool execution (200 steps, 6 loop detectors)
ghost_tools.py              Core tools — shell, files, web fetch, notifications
ghost_sandbox.py            Execution sandbox — resource limits, env scrubbing, process-group kill
ghost_edit_tools.py         Precise editing — edit_file (search/replace) + apply_patch (unified diff)
ghost_code_tools.py         Fast code search — ripgrep-backed grep + glob
ghost_git_tools.py          Structured git tools — status/diff/log/add/commit/branch/init on user repos
ghost_tool_builder.py       Dynamic tool system — create, install, validate tools at runtime
ghost_browser.py            Browser automation — Playwright with accessibility tree
ghost_memory.py             Basic memory — SQLite + FTS5
ghost_hybrid_memory.py      Hybrid memory — FTS5 + vector embeddings + temporal decay + MMR
ghost_auto_retrieval.py     Automatic pre-turn RAG — fused keyword+semantic retrieval with reranking
ghost_embeddings.py         Shared neural embedding provider (model2vec) with hash fallback
ghost_vector_memory.py      Vector memory — neural embeddings + NumPy ANN cosine search
ghost_session_memory.py     Session memory — auto-save conversation summaries
ghost_web_search.py         Web search — 6 providers with fallback and caching
ghost_web_fetch.py          Web fetch — 5-tier extraction pipeline with SSRF protection
ghost_vision.py             Vision — 5-provider image analysis
ghost_image_gen.py          Image generation — 3 providers
ghost_tts.py                Text-to-speech — Edge TTS, OpenAI, ElevenLabs
ghost_voice.py              Voice Wake + Talk Mode — wake word detection, STT, TTS playback
ghost_canvas.py             Canvas — visual output panel, session management, JS bridge
ghost_cron.py               Cron service — at/every/cron schedule types with missed job catch-up
ghost_skills.py             Skill loader — auto-discovery and trigger matching
ghost_plugins.py            Plugin system — hooks, custom tools, plugin data
ghost_evolve.py             Evolution engine — backup, validate, test, deploy, rollback
ghost_pr.py                 Adversarial PR review — separate LLM instance with 7 tools
ghost_autonomy.py           Autonomous growth — 14 routines, action items, self-repair
ghost_goals.py              Goal Engine — persistent long-horizon goals, GoalStore, LLM-callable tools
ghost_goal_executor.py      Deterministic Goal Executor — Python-controlled step loop, retry verification, quality check
ghost_model_dispatch.py     Budget-aware coding model selection for evolution & bug hunting
ghost_future_features.py    Feature backlog — prioritized queue with dedup and dependency ordering
ghost_providers.py          LLM providers — 7 providers with format adapters and fallback chains
ghost_auth_profiles.py      Auth store — API keys, OAuth tokens, credential sync (encrypted at rest)
ghost_secret_store.py       Secret encryption — Fernet-based encrypt/decrypt for secrets at rest
ghost_oauth.py              OAuth — Codex PKCE flow
ghost_integrations.py       Google Workspace + Grok integration
ghost_mcp.py                MCP client — connect external Model Context Protocol tool servers (stdio JSON-RPC)
ghost_webhooks.py           Webhook triggers — event-driven automation via HTTP POST
ghost_code_intel.py         Code intelligence — analysis, metrics, bug detection
ghost_data_extract.py       Data extraction — 15+ pattern types, table parsing
ghost_security_audit.py     Security audits — AI-driven with auto-fix
ghost_state_repair.py       State repair — validate and fix config/DB/logs on startup
ghost_setup_doctor.py       Setup doctor — preflight checks and safe auto-fixes
ghost_resource_manager.py   GPU/VRAM manager — smart eviction, serialized loading, watchdog
ghost_node_manager.py       Node manager — load, validate, install 22+ AI capability nodes
ghost_pipeline.py           Pipeline engine — chain node tools into multi-step workflows
ghost_node_registry.py      Node registry — community marketplace for AI nodes
ghost_node_sdk.py           Node SDK — scaffold, validate, test node projects
ghost_console.py            Event bus — real-time SSE streaming with ring buffer
ghost_interrupt.py          Mid-generation control — cancel or inject prompts during streaming
ghost_reasoning.py          Reasoning mode — /think directive for chain-of-thought
ghost_skill_registry.py     GhostHub — public skill registry with security scanning
ghost_skill_manager.py      Skill manager — install, validate, security-scan community skills
ghost_x_tracker.py          X/Twitter tracker — deduplication for social actions
ghost_credentials.py        Credential storage — structured service credentials with audit trail
ghost_supervisor.py         Process supervisor — crash recovery, auto-rollback after 5 crashes
ghost_platform.py           Cross-platform — macOS/Linux/Windows abstraction layer
  ghost_dashboard/            Flask web dashboard — 28+ pages, real-time SSE
  routes/                   32 API blueprint modules
  static/js/pages/          Frontend page modules (SPA, no build step)
  templates/                HTML shell
ghost_channels/             3 messaging channel implementations
ghost_nodes/                22 bundled AI capability nodes
skills/                     47 bundled skill definitions
SOUL.md                     Agent personality and development standards
USER.md                     User profile for personalization
```

---

## CLI

```bash
python ghost.py                        # Start daemon + dashboard
python ghost.py status                 # Daemon stats
python ghost.py log                    # Action history
python ghost.py context                # Current user context
python ghost.py cron list              # Scheduled jobs
python ghost.py soul show              # View personality
python ghost.py soul edit              # Edit SOUL.md
python ghost.py user show              # View user profile
python ghost.py reset                  # Show reset options
python ghost.py reset --all            # Full factory reset (backs up first)
python ghost.py reset --config         # Reset config & credentials only
python ghost.py reset --memory         # Clear memory databases only
python ghost.py dashboard              # Dashboard standalone
python ghost.py dashboard 8080         # Custom port
```

---

## Data Storage

All runtime data lives in `~/.ghost/`:

```
~/.ghost/
  config.json               Configuration
  .secret_key               Local encryption key for secrets at rest (chmod 600)
  auth_profiles.json        Provider credentials (API keys + OAuth tokens, encrypted at rest)
  memory.db                 SQLite memory database
  vector_memory.db          Semantic vector store (neural embeddings + model id per row)
  log.json                  Action history
  feed.json                 Activity feed
  ghost.pid                 Running daemon PID
  action_items.json         Things needing user attention
  growth_log.json           Autonomous improvement history
  future_features.json      Evolution backlog
  feature_changelog.json    Completed features log
  integrations.json         Google OAuth tokens
  mcp_servers.json          External MCP server definitions (see mcp_servers.example.json)
  channels.json             Channel configurations
  model_stats.json          GPU model load/eviction statistics
  coding_benchmarks.json    SWE-bench scores for coding model selection
  model_dispatch_cache.json Cached coding model selection (24h TTL)
  cron/jobs.json            Scheduled job definitions
  goals.json                Persistent user goals (status, plan, output, history)
  evolve/backups/           Project backups before self-modifications
  evolve/history.json       Evolution history (all deploys and rollbacks)
  nodes/                    User-installed AI nodes
  node_data/                Per-node persistent data
  models/                   Downloaded AI model cache
  audio/                    Generated TTS audio files
  voice/                    Voice capture and STT models
  canvas/                   Canvas session files (HTML/CSS/JS)
  generated_images/         Generated images
  memory/sessions/          Session summaries
  skills/                   User-created skills
  plugins/                  User plugins
  screenshots/              Captured screenshots
  state_backups/            State file backups from repair
```

---

## Reset / Fresh Start

Three ways to reset Ghost to a clean state. All resets create a timestamped backup (`~/.ghost.backup.<timestamp>/`) before wiping — your data is always recoverable.

| Method | Command | What It Wipes |
|---|---|---|
| **Installer** | `bash install.sh --fresh` | Moves entire `~/.ghost/` to backup, starts from scratch |
| **CLI — full** | `python ghost.py reset --all` | Everything in `~/.ghost/` — next start shows setup wizard |
| **CLI — config** | `python ghost.py reset --config` | Config, API keys, OAuth tokens — memory and skills preserved |
| **CLI — memory** | `python ghost.py reset --memory` | memory.db, vector_memory.db, session history — config preserved |
| **Dashboard** | Configuration page → "Reset Ghost" | Same three options via buttons in the UI |

Ghost must be stopped before running a full or config reset. The CLI checks for running processes and refuses if Ghost is still alive — this prevents file-lock failures on Windows.

---

## Configuration

Ghost stores configuration at `~/.ghost/config.json`. Every setting is editable from the dashboard with hot-reload (no restart needed).

| Key | Default | Description |
|---|---|---|
| `model` | `google/gemini-2.0-flash-001` | LLM model ID |
| `enable_tool_loop` | `true` | Multi-turn tool execution |
| `tool_loop_max_steps` | `40` | Max tool-loop iterations per task |
| `enable_evolve` | `true` | Allow self-modification |
| `evolve_auto_approve` | `false` | Skip approval for evolution changes |
| `enable_growth` | `true` | Autonomous improvement routines |
| `enable_browser_tools` | `true` | Browser automation |
| `enable_memory_db` | `true` | Persistent memory |
| `enable_cron` | `true` | Cron scheduler |
| `enable_voice` | `true` | Voice Wake + Talk Mode |
| `enable_canvas` | `true` | Canvas visual output panel |
| `enable_integrations` | `true` | Google/Grok integrations |
| `enable_mcp` | `true` | Connect external MCP tool servers from `~/.ghost/mcp_servers.json` |
| `enable_auto_retrieval` | `true` | Automatically retrieve + inject relevant memories before each chat turn |
| `enable_neural_embeddings` | `true` | Use local neural embeddings (model2vec) for semantic memory; auto-index existing memories on boot |
| `embedding_model` | `"minishlab/potion-base-8M"` | model2vec model for semantic embeddings (256-dim, CPU, no API key) |
| `dashboard_auth_token` | `""` | Optional token required to access the dashboard (also via `GHOST_DASHBOARD_TOKEN` env). Empty = open (local only) |
| `sandbox.enabled` | `true` | Master switch for the execution sandbox (resource limits, env scrubbing, process-group kill) |
| `sandbox.cpu_seconds` | `60` | POSIX `RLIMIT_CPU` for one-shot shell commands |
| `sandbox.file_size_mb` | `0` | POSIX `RLIMIT_FSIZE` cap on files written by commands (`0` = off, so large media/downloads aren't blocked) |
| `sandbox.env_mode` | `"scrub_secrets"` | Subprocess env handling: `full`, `scrub_secrets`, or `minimal` |
| `sandbox.isolation` | `"auto"` | Linux OS isolation: `auto` (use `bwrap` if present), `none`, or `bwrap` |
| `sandbox.network` | `"allow"` | `deny` blocks network for sandboxed commands (only enforceable via `bwrap`/`unshare`) |
| `coding_model_budget` | `"auto"` | Budget for coding tasks: `free`, `low`, `medium`, `high`, `auto`, or $/MTok number |
| `coding_model_override` | `null` | Force a specific model for coding tasks (bypasses dispatcher) |
| `min_swe_bench_score` | `78.0` | Minimum SWE-bench score for coding model selection |

---

## Cross-Platform

Ghost runs on **macOS**, **Linux**, and **Windows**. No system-level dependencies — all Python packages are pip-installable with pure-Python fallbacks where system libraries are needed.

- **Install & launch scripts**: `install.sh` / `start.sh` / `stop.sh` (macOS/Linux) and `install.ps1` / `start.bat` / `stop.bat` (Windows)
- **Process management**: `SIGTERM` on Unix, `taskkill` on Windows, with cross-platform detached-process and process-group helpers
- **Notifications**: `osascript` (macOS), `notify-send` (Linux), PowerShell balloon tips (Windows)
- **Audio playback**: `afplay` (macOS), `mpv`/`paplay`/`aplay` (Linux), PowerShell `SoundPlayer` (Windows), with `sounddevice` fallback
- **LLM platform context**: Ghost injects OS, Python version, shell, and path style into the system prompt so tool outputs match the user's platform

---

## Security

Ghost takes security seriously for an autonomous agent:

- **Secrets encrypted at rest** — Provider API keys, OAuth tokens, and saved credentials are encrypted with Fernet (AES-128-CBC + HMAC). The data key lives at `~/.ghost/.secret_key` (chmod 600) or via the `GHOST_SECRET_KEY` env var. Legacy plaintext files are auto-migrated on first load; secret files are written `0600`. Degrades gracefully to plaintext only if `cryptography` is unavailable.
- **Optional dashboard authentication** — Set `GHOST_DASHBOARD_TOKEN` (or `dashboard_auth_token` in config) to require a token before binding the dashboard to a non-loopback host. Supports `Authorization: Bearer`, `X-Ghost-Token`, `?token=`, or a sign-in page that sets an HttpOnly cookie. Off by default so local usage is unchanged.
- **Tool shadowing prevention** — Reserved tool names can't be overwritten by plugins or skills
- **Codebase write protection** — `file_write`, `edit_file`, and `apply_patch` can't modify Ghost's own source files (self-modification goes through the evolution pipeline)
- **Execution sandbox** — Shell commands run with POSIX resource limits (CPU, file size, core dumps), secret-scrubbed environments, and process-group kill on timeout; optional Linux `bwrap` OS isolation. See [Execution Sandbox](#execution-sandbox). Configurable under the `sandbox` key.
- **Command blocklist** — Genuinely destructive patterns (`rm -rf /`, `mkfs`, fork bombs, …) are blocked in `shell_exec`/sessions. (Note: the `allowed_commands` allowlist is *not* enforced at runtime — Ghost relies on the blocklist + sandbox instead.)
- **Dangerous interpreter policy** — For autonomous (cron) callers, Python/pip execution is gated with per-command deny flags and optional workspace requirements
- **Skill security scanning** — 34+ regex patterns across 5 threat categories before installation
- **Node security scanning** — AST parsing and dangerous code pattern detection
- **Channel DM policies** — Open, allowlist, blocklist, or block per channel
- **Webhook HMAC verification** — Per-trigger `X-Hub-Signature-256` validation
- **SSRF protection** — Web fetch blocks private IPs and local addresses
- **Prompt injection defense** — HTML sanitization and content filtering on web-fetched data
- **Audit logging** — Every credential save, cloud provider update, and security event is logged

---

## Disclaimer

Ghost is open-source software provided as-is. It can execute shell commands, modify files, browse the web, and send messages on your behalf. **You are responsible for how you use it.** Ghost is not financial advice, not a licensed professional service, and not liable for any actions taken based on its output. Review what it does. Use at your own risk.

## License

MIT
