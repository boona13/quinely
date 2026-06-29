---
name: blogwatcher
description: "Monitor blogs and RSS/Atom feeds for updates using the blogwatcher CLI"
triggers: ["rss", "blog", "feed", "subscribe", "blogwatcher", "articles"]
tools: ["shell_exec", "web_fetch"]
priority: 5
---
You are Quinely monitoring blogs and RSS feeds. Use `blogwatcher` to track blog/RSS/Atom feed updates.

## Setup

- Install (Go): `go install github.com/Hyaxia/blogwatcher/cmd/blogwatcher@latest`

## Common Commands

### Add a Blog

```bash
blogwatcher add "My Blog" https://example.com
```

### List Tracked Blogs

```bash
blogwatcher blogs
```

### Scan for Updates

```bash
blogwatcher scan
```

### List Articles

```bash
blogwatcher articles
```

### Mark Articles Read

```bash
blogwatcher read 1          # Mark specific article read
blogwatcher read-all        # Mark all articles read
```

### Remove a Blog

```bash
blogwatcher remove "My Blog"
```

## Example Output

```
$ blogwatcher blogs
Tracked blogs (1):

  xkcd
    URL: https://xkcd.com
```

```
$ blogwatcher scan
Scanning 1 blog(s)...

  xkcd
    Source: RSS | Found: 4 | New: 4

Found 4 new article(s) total!
```

## Reading Full Articles

When the user wants to read a specific article from the feed:
1. Use `web_fetch` on the article URL — it extracts clean markdown from most blogs and news sites.
2. If `web_fetch` returns limited content (< 200 chars), fall back to `summarize` CLI or browser.

## Notes

- Use `blogwatcher <command> --help` to discover flags and options.
