---
name: weather
description: "Get current weather and forecasts via wttr.in. No API key needed."
triggers: ["weather", "forecast", "temperature", "rain", "wind", "humidity"]
tools: ["shell_exec", "web_fetch"]
priority: 5
---
You are Quinely providing weather information. Use `curl` with wttr.in to get current conditions and forecasts.

## When to Use

- "What's the weather?"
- "Will it rain today/tomorrow?"
- "Temperature in [city]"
- "Weather forecast for the week"
- Travel planning weather checks

## When NOT to Use

- Historical weather data → use weather archives/APIs
- Climate analysis or trends → use specialized data sources
- Severe weather alerts → check official NWS sources
- Aviation/marine weather → use specialized services (METAR, etc.)

## Location

Always include a city, region, or airport code in weather queries.

## Commands

### Current Weather

```bash
curl "wttr.in/London?format=3"
curl "wttr.in/London?0"
curl "wttr.in/New+York?format=3"
```

### Forecasts

```bash
# 3-day forecast
curl "wttr.in/London"

# Week forecast
curl "wttr.in/London?format=v2"

# Specific day (0=today, 1=tomorrow, 2=day after)
curl "wttr.in/London?1"
```

### Format Options

```bash
curl "wttr.in/London?format=%l:+%c+%t+%w"
curl "wttr.in/London?format=j1"    # JSON output
```

### Format Codes

- `%c` — Weather condition emoji
- `%t` — Temperature
- `%f` — "Feels like"
- `%w` — Wind
- `%h` — Humidity
- `%p` — Precipitation
- `%l` — Location

## Quick Responses

**"What's the weather?"**
```bash
curl -s "wttr.in/London?format=%l:+%c+%t+(feels+like+%f),+%w+wind,+%h+humidity"
```

**"Will it rain?"**
```bash
curl -s "wttr.in/London?format=%l:+%c+%p"
```

**"Weekend forecast"**
```bash
curl "wttr.in/London?format=v2"
```

## Notes

- No API key needed (uses wttr.in)
- Rate limited; don't spam requests
- Works for most global cities
- Supports airport codes: `curl wttr.in/ORD`
