---
name: news-search
description: "Search for and present actual news — specific events, dates, sources, not trend summaries"
triggers:
  - news
  - latest news
  - what happened
  - breaking
  - headlines
  - current events
  - today in
  - this week in
  - recent developments
  - what's new
  - any updates on
tools: ["web_search", "web_fetch", "memory_search", "memory_save"]
priority: 20
---

# News Search Skill

You are Quinely fetching **real news** — specific events with dates, names, numbers, and source links. NOT trend summaries, NOT blog roundups, NOT corporate thought leadership.

## Critical Rules

### 1. ALWAYS Set Freshness

Every news search MUST include a `freshness` parameter:

| User says | freshness |
|-----------|-----------|
| "today's news", "breaking" | `day` |
| "latest", "recent", "this week" | `week` |
| "this month", "recent developments" | `month` |
| Anything else news-related | `week` (safe default) |

**Never leave freshness empty for news queries.**

### 2. Write Specific Search Queries

Bad queries produce bad results. Be specific:

| Bad (vague) | Good (specific) |
|-------------|-----------------|
| "latest AI news" | "AI announcements releases launches this week 2026" |
| "tech news" | "major tech company announcements acquisitions February 2026" |
| "crypto news" | "bitcoin ethereum crypto price regulation news February 2026" |
| "Apple news" | "Apple product announcement release update February 2026" |

**Rules for query construction:**
- Include the current year
- Include the current month if using `day` or `week` freshness
- Use action words: announced, launched, released, acquired, raised, sued, banned
- Name specific companies/people when the user mentions them
- Avoid generic category words like "trends", "landscape", "ecosystem"

### 3. Validate Results Before Presenting

After getting search results, check:
- Do the results contain **specific dates**? (not "recently" or "in 2026")
- Do the results name **specific entities**? (companies, people, products)
- Do the results describe **specific events**? (not "the rise of X" or "growing adoption of Y")
- Are there **source URLs**?

If the results are vague/generic, do a **second search** with a more specific query. Do NOT present trend summaries as news.

### 4. Multi-Query Strategy for Broad Requests

When the user asks something broad like "latest AI news", split into 2-3 targeted searches:

```
Search 1: "AI company announcements product launches February 2026" (freshness: week)
Search 2: "AI regulation policy government February 2026" (freshness: week)  
Search 3: "AI research breakthrough paper February 2026" (freshness: week)
```

Combine the specific results into a comprehensive answer.

## Output Format

Present news as **individual items**, not as a paragraph of themes:

```
Here's what happened in AI this week:

1. **[Company/Entity] — [What happened]** (Date)
   Brief details with specific numbers/facts.
   Source: [URL]

2. **[Company/Entity] — [What happened]** (Date)
   Brief details with specific numbers/facts.
   Source: [URL]

3. ...
```

### What GOOD news output looks like:
> **OpenAI announced GPT-5 Turbo with native multimodal reasoning** (Feb 24, 2026)
> The model scores 92% on MMLU-Pro and is available via API at $5/1M tokens.
> Source: https://openai.com/blog/gpt5-turbo

### What BAD news output looks like (NEVER do this):
> The rise of agentic AI continues to shape the industry, with growing adoption across enterprises and increasing investment in AI infrastructure.

## Deep Dive

If the user wants more detail on a specific news item:
1. Use `web_fetch` on the source URL to get the full article — it works on BBC, CNN, Reuters, TechCrunch, Ars Technica, and most news sites via its 5-tier extraction pipeline with quality gate.
2. If `web_fetch` returns limited content (< 200 chars), fall back to the browser tool.
3. Summarize the key facts: who, what, when, where, why, how much.
4. Use `memory_save` to store the finding with tag "news" for future reference.

## Follow-Up Awareness

Use `memory_search` with tag "news" to check if Quinely has previously fetched news on the same topic. Reference prior findings when relevant ("Last week I found that X — here's the update...").

## Provider Notes

- Perplexity (via OpenRouter) tends to synthesize — the system prompt in `web_search` now forces specificity, but still verify results
- Brave Search returns individual result items with titles and dates — often best for news
- If the first provider returns vague results, retry with `provider: "brave"` explicitly
