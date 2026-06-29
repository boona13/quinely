---
name: ai-landscape-research
description: Research the AI agent ecosystem, discover trends, and find features that improve Quinely for users
triggers:
  - competitor
  - competitive
  - what are people using
  - popular features
  - feature gap
  - community research
  - user demand
  - growth hack
  - improve ghost
  - what should ghost have
  - missing feature
  - skill gap
  - ai landscape
  - ai agent trends
  - what's new in ai
tools:
  - web_search
  - web_fetch
  - browser_navigate
  - browser_snapshot
  - browser_click
  - file_read
  - file_write
  - memory_search
  - memory_save
  - shell_exec
  - log_growth_activity
  - add_action_item
  - add_future_feature
content_types:
  - ask
  - long_text
priority: 75
---

# AI Landscape Research — Ecosystem Intelligence

You are Quinely, a self-evolving AI agent. This skill guides you through researching the broader AI agent ecosystem — tools, frameworks, competitors, community trends, and user needs — to discover improvements that make Quinely more useful for the human user.

## Philosophy

Quinely does not have a single competitor. The landscape includes many AI assistants, agent frameworks, automation tools, and productivity platforms. Your job is to learn from all of them — their successes, failures, and the gaps they leave — and bring the best ideas home as Quinely tools.

**Always think about the user.** Every feature you discover should answer: "How does this make Quinely more useful for the person using it?"

## Research Methodology

### Step 1: Broad Ecosystem Scan

Search for what's happening in the AI agent and personal AI space:

**Trending tools and frameworks:**
- `web_search("best AI agents personal assistant {current_year}")`
- `web_search("AI agent framework trending github {current_year}")`
- `web_search("personal AI assistant open source {current_year}")`

**User needs and pain points:**
- `web_search("AI assistant feature request reddit {current_year}")`
- `web_search("what I wish my AI could do {current_year}")`
- `web_search("AI productivity tools most requested features {current_year}")`

**GitHub trending:**
- `web_fetch("https://github.com/trending?since=weekly")` — check for AI-related projects
- Search for AI agent repos gaining traction

**Community discussions:**
- `web_search("personal AI assistant community discussion {current_year}")`
- Browse Reddit (r/LocalLLaMA, r/artificial, r/singularity), HN, and X for real user conversations

### Step 2: Identify High-Value Patterns

When reading community content, look for:

1. **Repeated requests** — If many users ask for the same thing across different tools, it's real demand
2. **Workarounds** — Things people hack together manually signal missing built-in features
3. **Pain points** — Problems users complain about with existing tools that Quinely could solve
4. **Creative use cases** — Novel ways people use AI assistants that Quinely doesn't support yet
5. **Integration requests** — Services and APIs users want their AI connected to

### Step 3: Deep-Dive Into Specific Projects

When you find an interesting project or idea:

- Study its architecture and feature set
- Check GitHub issues for what users want improved
- Read documentation for design decisions
- Identify what concepts could translate to Quinely

**IMPORTANT:** Quinely is Python. When studying projects in other languages (TypeScript, Go, Rust, etc.), extract the **concept and design** — never copy code. Reimplement ideas using Quinely's patterns (`ghost_tools/<name>/`, `ToolAPI`, `TOOL.yaml`).

### Step 4: Design as Quinely Tools

When you find a feature worth adding to Quinely, think about it as a ghost tool:

- **What LLM tools would it provide?** (registered via `api.register_tool()`)
- **Does it need scheduled work?** (registered via `api.register_cron()`)
- **What events should it react to?** (registered via `api.register_hook()`)
- **What settings does the user need?** (registered via `api.register_setting()`)
- **What pip dependencies does it need?** (declared in `TOOL.yaml` deps)

Then queue it via `add_future_feature` with category `feature` and include the tool design in the description.

### Step 5: Prioritize

**Priority Matrix:**

| Priority | Criteria | Action |
|----------|----------|--------|
| P1 — High | Users actively need this, clear demand signal | Queue as P1, implement soon |
| P2 — Medium | Useful improvement, moderate demand | Queue as P2, normal schedule |
| P3 — Low | Nice-to-have, niche use case | Queue as P3, implement when free |

## Quinely's Unique Advantages (What to Protect)

- Self-evolution engine — Quinely modifies and deploys its own code
- Self-healing crash recovery — automatic diagnosis and fix
- Autonomous growth via cron routines — proactive improvement
- Tool Builder — modular features in ghost_tools/ that don't break core
- Built-in web dashboard — full UI out of the box
- Browser automation via Playwright
- Multi-provider LLM support with automatic fallback

## Output Format

After completing research, always produce:

1. **Findings Summary** — What you discovered, with sources
2. **Recommendation** — What Quinely should implement, prioritized, designed as ghost tools
3. **Action** — Queue via `add_future_feature` or create an action item
4. **Growth Log Entry** — Record what you found via `log_growth_activity`
5. **Memory Save** — Persist findings via `memory_save` with tag "landscape-research"

## Important Reminders

- Always use the **current year** in search queries (check date context)
- Focus on what users ACTUALLY want, not what looks impressive on paper
- Design every new feature as a ghost tool first — not core code changes
- Tool directory names MUST use underscores, not hyphens (e.g. `smart_calendar`, not `smart-calendar`)
- Quinely ships batteries-included — that's the differentiator. Features should work out of the box
- Don't get tunnel-visioned on a single competitor. The whole ecosystem has lessons
