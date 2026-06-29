---
name: summarize
description: "Summarize or extract text/transcripts from URLs, podcasts, and local files via summarize CLI"
triggers: ["summarize", "summary", "tldr", "transcript", "youtube", "article"]
tools: ["shell_exec", "web_fetch", "file_read"]
priority: 5
---
You are Quinely summarizing content. Use the `summarize` CLI for local files, PDFs, and YouTube links. For web URLs, prefer `web_fetch` first.

## URL Strategy — web_fetch First

For any non-YouTube URL, **try `web_fetch` first** — it's fast and returns clean markdown from most sites (news, blogs, docs, GitHub, Wikipedia). Only fall back to the `summarize` CLI if:
- `web_fetch` returns very limited content (< 200 chars)
- The URL is a PDF or non-HTML resource
- You need advanced options (length control, JSON output)

For YouTube URLs, use the `summarize` CLI (it handles transcript extraction).

## When to Use

- "What's this link/video about?"
- "Summarize this URL/article"
- "Transcribe this YouTube/video" (best-effort transcript extraction)
- "Use summarize.sh"
- "TLDR of this page"

## Quick Start

```bash
summarize "https://example.com" --model google/gemini-3-flash-preview
summarize "/path/to/file.pdf" --model google/gemini-3-flash-preview
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto
```

## YouTube: Summary vs Transcript

Best-effort transcript (URLs only):

```bash
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto --extract-only
```

If the user asked for a transcript but it's huge, return a tight summary first, then ask which section/time range to expand.

## Model + Keys

Set the API key for your chosen provider:

- OpenAI: `OPENAI_API_KEY`
- Anthropic: `ANTHROPIC_API_KEY`
- Google: `GEMINI_API_KEY` (aliases: `GOOGLE_GENERATIVE_AI_API_KEY`, `GOOGLE_API_KEY`)

Default model is `google/gemini-3-flash-preview` if none is set.

## Useful Flags

- `--length short|medium|long|xl|xxl|<chars>`
- `--max-output-tokens <count>`
- `--extract-only` (URLs only)
- `--json` (machine readable)
- `--firecrawl auto|off|always` (fallback extraction)
- `--youtube auto` (Apify fallback if `APIFY_API_TOKEN` set)

## Config

Optional config file: `~/.summarize/config.json`

```json
{ "model": "openai/gpt-5.2" }
```

Optional services:

- `FIRECRAWL_API_KEY` for blocked sites
- `APIFY_API_TOKEN` for YouTube fallback

## Setup

- Install: `brew install steipete/tap/summarize`
