---
name: future-features
triggers:
  - future feature
  - feature backlog
  - implement feature
  - add to backlog
  - queue feature
  - pending features
  - feature queue
  - evolution queue
  - serial evolution
  - code change
  - queue bug fix
  - queue improvement
tools:
  - add_future_feature
  - list_future_features
  - get_future_feature
  - approve_future_feature
  - start_future_feature
  - complete_future_feature
  - fail_future_feature
  - get_feature_stats
priority: high
content_types:
  - question
  - command
---

# Future Features — Serial Evolution Queue

The central queue for ALL code changes in Quinely. Every evolution — features, bug fixes, security patches, refactors, improvements — flows through this queue and is processed serially by the Evolution Runner.

## Why Serial?

`evolve_deploy` restarts the entire Quinely process. If two loops evolve concurrently, one deploy kills the other mid-execution, losing work. The serial queue eliminates this by design:
- Only the **Feature Implementer** (Evolution Runner) has `evolve_*` tools.
- All other routines and user chat queue changes via `add_future_feature`.
- One item is processed at a time. After deploy, Quinely restarts and picks up the next item.

## Priority Levels

- **P0 (User-requested)**: Requires user approval. Triggers Evolution Runner immediately once approved.
- **P1 (Urgent)**: Bug fixes, security patches. Triggers Evolution Runner immediately via `cron.fire_now()`.
- **P2 (Normal)**: Standard improvements. Processed on the regular 6-hour schedule.
- **P3 (Low)**: Low priority. Processed when nothing else is queued.

## Categories

Each queue item has a category:
- **feature**: New functionality
- **bugfix**: Bug fix (typically P1, source=bug_hunter)
- **security**: Security hardening (typically P1, source=security_patrol)
- **refactor**: Code cleanup
- **improvement**: Enhancement to existing feature
- **soul_update**: SOUL.md changes (from Soul Evolver)

## Sources

Every routine queues changes instead of evolving directly:
- **Tech Scout**: AI/tech news discoveries → `add_future_feature(source='tech_scout')`
- **Bug Hunter**: Error fixes → `add_future_feature(priority='P1', category='bugfix', source='bug_hunter')`
- **Security Patrol**: Vulnerability fixes → `add_future_feature(priority='P1', category='security')`
- **AI Landscape Research**: Ecosystem trends and user needs → `add_future_feature(source='competitive_intel')`
- **Skill Improver**: Skill improvements → `add_future_feature(category='improvement')`
- **Soul Evolver**: SOUL.md updates → `add_future_feature(category='soul_update')`
- **User Chat**: User requests → `add_future_feature(priority='P0', source='user_request')`
- **Manual**: Direct addition via dashboard

## Workflow

1. **Discover/Request**: Any routine or user chat identifies a needed change.
2. **Queue**: Call `add_future_feature(title, description, priority, source, category)`.
3. **Trigger**: P0/P1 items fire the Evolution Runner immediately. P2/P3 wait for schedule.
4. **Implement**: Evolution Runner picks highest-priority pending item, evolves, tests, deploys.
5. **Restart**: Quinely restarts. Next item picked up on next run.
6. **Complete**: Marked as done, added to changelog.

## Deploy Safety

Before writing the `deploy_pending` marker, the Evolution Runner waits up to 30 seconds for other running cron jobs to finish. This prevents killing non-evolve work (e.g., Tech Scout doing a web search) during the restart.

## Dependency Management

When the Evolution Runner or any routine runs `pip install <package>` via `shell_exec`, `requirements.txt` is **automatically updated** with the installed version (appended as `package>=version`). **Do NOT manually edit `requirements.txt` after pip install** — the auto-sync handles it and manual edits will create duplicates. This ensures new dependencies are always tracked and survive fresh installs.

## How to Queue a Code Change (for chat)

Since you do NOT have evolve tools in chat, use:
```
add_future_feature(
  title="Brief description",
  description="What and why — the problem/opportunity",
  affected_files="ghost_foo.py, ghost_dashboard/routes/bar.py",
  proposed_approach="Step-by-step plan: 1) Add function X to ghost_foo.py, 2) ...",
  priority="P0",           # P0 for user requests
  source="user_request",
  category="feature"       # or bugfix, security, refactor, improvement, soul_update
)
```

The `affected_files` and `proposed_approach` fields are the handoff — they tell the Evolution Runner exactly what to do WITHOUT re-investigating your work.

Check status with `list_future_features` or `get_feature_stats`.

## Dashboard

See the Future Features page in the Quinely dashboard for full queue UI — add, approve, reject, and track items.
