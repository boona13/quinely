---
name: ui-development
description: "UI/UX development standards for Quinely dashboard — design system, verification, and quality rules"
triggers:
  - dashboard
  - ui
  - ux
  - frontend
  - page
  - button
  - style
  - css
  - layout
  - design
  - component
  - sidebar
  - chat
  - toggle
  - modal
  - card
  - form
  - input
  - theme
  - dark mode
  - responsive
  - animation
  - attach
  - upload
tools:
  - file_read
  - file_write
  - browser
  - evolve_plan
  - evolve_apply
  - evolve_test
  - evolve_deploy
priority: 85
---

# UI/UX Development Standards

Follow these rules for ALL dashboard or frontend work. No exceptions.

## 1. Completeness Rule — NEVER Ship Half-Done UI

A UI change is NOT complete until ALL of these exist:
- **Backend**: API endpoints / route handlers for new functionality
- **Frontend JS**: Event handlers, DOM creation, state management, API calls
- **CSS**: Styles for EVERY new element (hover states, transitions, responsive)
- **Wiring**: Backend ↔ Frontend data flow fully connected
- **Verification**: You browsed to `localhost:3333` and visually confirmed it works

**Checklist before calling `evolve_test`:**
- [ ] Every new HTML element has corresponding CSS
- [ ] Every new CSS class is actually used in the HTML/JS
- [ ] Every new API endpoint is called by the frontend
- [ ] Every new function is actually invoked (not just defined)
- [ ] Every variable is declared before use
- [ ] State flows: initialization → user action → API call → response → DOM update

## 2. Visual Verification — MANDATORY for UI Changes

After `evolve_deploy` completes and the system restarts, you MUST:

```
1. browser(action='navigate', url='http://localhost:3333/#<page>')
2. browser(action='snapshot')
3. Verify all new elements appear in the snapshot
4. browser(action='screenshot') to capture visual state
5. Test interaction: click buttons, fill inputs, verify responses
6. If ANYTHING is missing or broken → fix it immediately with another evolution
```

**NEVER call `task_complete` on a UI task without browsing to verify.**

If the page doesn't load or elements are missing:
- Check `browser(action='console')` for JavaScript errors
- Read the JS file to find the bug
- Fix with a new `evolve_plan` → `evolve_apply` → `evolve_test` → `evolve_deploy` cycle
- Browse again to verify the fix

## 3. Quinely Dashboard Design System

### Color Tokens

Use these exact values — never invent new colors:

```css
/* Backgrounds */
--bg-darkest: #0a0a14;
--bg-card: #10101c;
--bg-input: #161625;

/* Borders */
--border-default: rgba(30, 30, 48, 0.5);
--border-hover: rgba(139, 92, 246, 0.3);
--border-active: rgba(139, 92, 246, 0.5);

/* Quinely Purple (primary) */
--purple-500: #8b5cf6;
--purple-400: #a78bfa;
--purple-glow: rgba(139, 92, 246, 0.2);

/* Text */
--text-heading: #ffffff;
--text-body: #d4d4d8;
--text-muted: #a1a1aa;
--text-dim: #71717a;

/* Status */
--success: #10b981;
--warning: #f59e0b;
--error: #ef4444;
```

### Component Patterns

**Cards**: `background: #10101c`, `border: 1px solid rgba(30, 30, 48, 0.5)`, `border-radius: 0.75rem`, `padding: 1rem 1.25rem`

**Buttons**:
- Primary: `.btn-primary` — purple bg, white text
- Quinely: `.btn-ghost` — transparent, text hover
- Danger: `.btn-danger` — red variant
- Size: `.btn-sm` for compact, default for standard

**Inputs**: Dark bg `#161625`, border transitions to purple on focus, `border-radius: 0.5rem–0.75rem`

**Transitions**: Always `transition: all 150ms` or `200ms`. Never instant state changes.

**Badges**: `.badge .badge-green`, `.badge-yellow`, `.badge-red`

### Naming Conventions

- CSS classes: `.pagename-element` (e.g., `.chat-input`, `.cron-card`, `.evolve-history`)
- JS variables: camelCase, suffix with `El` for DOM elements (e.g., `messagesEl`, `sendBtn`)
- API routes: `/api/<section>/<action>` (e.g., `/api/chat/send`, `/api/cron/list`)

## 4. CSS Modification Rules

**NEVER replace an entire CSS file.** The evolve engine blocks this.

To add new styles:
```
evolve_apply(evolution_id, file_path='ghost_dashboard/static/css/dashboard.css', patches=[
    {"old": "LAST_FEW_LINES_OF_FILE", "new": "LAST_FEW_LINES_OF_FILE\n\n/* New section */\n.my-class { ... }"}
])
```

To modify existing styles:
```
evolve_apply(..., patches=[
    {"old": ".existing-class {\n  color: red;\n}", "new": ".existing-class {\n  color: blue;\n}"}
])
```

**Read the CSS file first** with `file_read` to get exact current content for patch targets.

## 5. JavaScript Modification Rules

**Every function you add must be called somewhere.** Dead code = bug.

When modifying JS pages:
1. `file_read` the entire JS file first
2. Identify exact insertion points (functions, event listeners, DOM refs)
3. Use `patches` with enough context for unique matching
4. Verify: every new variable is declared (`let`/`const`), every new function is invoked, every new DOM element has an event listener or is rendered

**Common mistakes to avoid:**
- Adding attachment handling code but forgetting the file input element
- Adding API calls but forgetting the DOM elements that display responses
- Modifying `sendMessage()` to use variables that don't exist in scope
- Adding CSS classes in JS but forgetting to add them to the CSS file

## 6. SPA Architecture

The dashboard is a Single Page Application:
- `app.js` handles routing via `window.location.hash`
- Each page is a module in `static/js/pages/` exporting `render(container)`
- `render()` receives the `#main-content` container and builds the entire page
- Navigation doesn't reload — only `render()` is called again
- Status polling in `app.js` runs independently of pages

When creating a new page:
1. JS module must export `render(container)`
2. Register in `app.js` navigate function
3. Add sidebar link in `templates/index.html`
4. All state is local to `render()` scope (closures)

## 7. Server-Sent Events (SSE) Pattern

Chat uses SSE for real-time streaming:
- `EventSource` connects to `/api/chat/stream/<messageId>`
- Events: `step` (tool progress), `done` (completion), `error`, `approval_needed`
- On disconnect: fallback to polling → restart detection → recovery
- After restart: `ghost:restarted` event fired by `app.js` when status poll succeeds

When modifying chat streaming:
- Keep the `onerror` handler robust — server may restart mid-stream
- The restart banner and recovery flow must be preserved
- Test by sending a message that triggers `evolve_deploy` and verifying restart UX
