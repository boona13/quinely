---
name: ghost-goals
description: "Create and manage persistent multi-step goals with autonomous execution — recurring tasks, long-horizon projects, weekly reports"
triggers:
  - goal
  - create goal
  - set a goal
  - recurring task
  - every monday
  - every week
  - every day
  - weekly
  - daily
  - monthly
  - recurring
  - autonomous task
  - long term
  - ongoing task
  - track over time
  - monitor weekly
  - list goals
  - show goals
  - my goals
  - pause goal
  - abandon goal
  - resume goal
tools:
  - run_goal_engine
  - goal_create
  - goal_plan
  - goal_step_done
  - goal_step_fail
  - goal_add_observation
  - goal_complete
  - goal_list
  - goal_get
  - goal_pause
  - goal_resume
  - goal_abandon
  - goal_set_output
  - goal_set_delivery
  - web_search
  - web_fetch
  - memory_search
  - memory_save
  - notify
  - channel_send
  - file_write
priority: 20
---

# Quinely Goal Engine

Quinely has a persistent Goal Engine for long-horizon, multi-step, and recurring tasks.

## When to use Goal tools (NOT a regular cron job)

Use goal tools when:
- The user wants something done **repeatedly over time** (weekly reports, daily monitors)
- The task has **multiple steps** where later steps need results from earlier ones
- The task needs to **accumulate data over time** (track trends, compare week-over-week)
- The task is **too long** to run entirely in one session

## CRITICAL — NEVER NARRATE, ALWAYS CALL THE TOOL

**This is the most important rule in this skill.**

When asked to create, execute, or manage a goal — CALL THE TOOL. Do not describe what you would do.

WRONG (narrating):
> "I would call goal_create with the title 'Weekly Digest' and then set up the plan..."
> "I executed step 1 by searching the web and found 5 stories..."
> "I saved the output to the goal store."

RIGHT (doing):
- Call `goal_create(...)` → get back a goal_id
- Call `goal_plan(goal_id, steps=[...])` → plan is stored
- Call `web_search(...)` → actually search
- Call `goal_step_done(goal_id, step_id, result=...)` → step is marked done
- Call `goal_set_output(goal_id, output=...)` → output is saved
- Call `goal_complete(goal_id)` → cycle is closed

**If your response text says "I saved..." or "I called..." without a matching tool call in your actual tool use — you have failed. Go back and call the tool.**

Verification rule: before ending your response, count every action you described. Each described action must have a corresponding real tool call. If any are missing, make those tool calls now.

## Core workflow

### Creating a new goal
ALWAYS call `goal_create()` when a user asks you to do something recurring or persistent:
```
goal_create(
    title="Weekly AI News Digest",
    goal_text="Every Monday, search top 5 AI news stories, summarize each, save to memory",
    recurrence="0 9 * * 1"   # cron: every Monday at 9am
)
```
Common recurrence patterns:
- Every Monday 9am: `0 9 * * 1`
- Every day 8am: `0 8 * * *`
- Every weekday 9am: `0 9 * * 1-5`
- First of month: `0 9 1 * *`
- Every 6 hours: `0 */6 * * *`
- One-shot (no recurrence): leave recurrence empty

### After creating a goal — immediately plan it
Don't wait for the cron executor. Plan the goal right away:
```
goal_plan(goal_id, steps=[
    "Search for top 5 AI news stories from the past week",
    "Fetch and summarize each story in 2-3 sentences",
    "Save the complete digest to memory with tag 'ai-news-weekly'",
    "Compile full digest and call goal_set_output with the complete markdown content, then call goal_complete"
])
```

### Delivery — how the user receives results
When the user specifies how they want the output, call `goal_set_delivery` right after `goal_create`:
```
goal_set_delivery(goal_id, delivery="notify")      # push notification summary
goal_set_delivery(goal_id, delivery="discord")     # post to Discord channel
goal_set_delivery(goal_id, delivery="telegram")    # send via Telegram
goal_set_delivery(goal_id, delivery="file:~/digests/ai-news.md")  # write to file
goal_set_delivery(goal_id, delivery="chat")        # post in dashboard chat feed
goal_set_delivery(goal_id, delivery="")            # Goals dashboard only (default)
```

### Listing / checking goals
```
goal_list()                          # all goals
goal_list(status="active")           # only active
goal_list(status="pending_plan")     # awaiting a plan
goal_get(goal_id)                    # full details with plan + observations
```

### Managing goals
```
goal_pause(goal_id)     # temporarily pause
goal_resume(goal_id)    # resume a paused goal
goal_abandon(goal_id)   # permanently stop
```

## Key concepts

**goal_set_output is MANDATORY**: The last step of every goal cycle MUST call `goal_set_output(goal_id, output=<full content>)` before `goal_complete()`. This is the actual result the user sees in the Goals dashboard — the complete digest, full report, all findings. NOT a one-liner. Include everything.

**Observations = working memory**: Each step can save findings that persist across ALL future runs.
This is what makes goals different from a plain cron job — the executor reads prior observations
and builds on them (e.g. "last week prices were X, this week they are Y — up 5%").

**One step per executor run**: The `goal_executor` cron fires every 30 minutes and runs ONE
step per goal. This keeps each cron run short and focused.

**Recurring goals auto-reset**: When `goal_complete()` is called on a goal with a recurrence,
all steps reset to pending for the next cycle. The observations persist.

## When asked to "run a goal now" / "execute manually"

If the user asks you to execute a goal right now (not wait for the cron), run ALL steps in one session. For each step:
1. **Actually do the work** — call `web_search`, `web_fetch`, etc.
2. **Call `goal_step_done`** with the real result string immediately after
3. When all steps done: call `goal_set_output` with the FULL content, then `goal_complete`

Do NOT do the steps out of order or batch them all at the end. Call `goal_step_done` after EACH step, one at a time.

**Sequence for a 5-step goal (do in this exact order):**
```
web_search(...)                            ← do the actual work
goal_step_done(goal_id, "s1", result=...) ← mark it done immediately
web_fetch(...)                             ← do step 2 work
goal_step_done(goal_id, "s2", result=...) ← mark done immediately
... (continue for each step) ...
goal_set_output(goal_id, output=<full>)   ← save the deliverable
goal_step_done(goal_id, "s5", result=...) ← mark final step done
goal_complete(goal_id, summary=...)       ← close the cycle
```

## Response format

After calling `goal_create`:
- Confirm the goal was created with its ID
- Tell the user the recurrence schedule in plain English
- Immediately call `goal_plan()` with concrete steps
- Confirm the plan is set and the executor will run it automatically
