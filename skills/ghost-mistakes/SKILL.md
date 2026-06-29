---
name: ghost-mistakes
description: Instructs Quinely to query past mistakes from memory before evolution or feature work
triggers:
  - evolve
  - evolution
  - implement
  - feature
  - evolve_plan
  - evolve_apply
  - self-modification
  - bug fix
  - wiring fix
priority: 90
---

# Mistake Memory — Query Before You Build

Quinely has a library of past LLM mistakes stored in persistent memory (type: `mistake`).

**Before starting any evolution or feature implementation, you MUST:**

1. Call `memory_search(query="<keywords relevant to your current task>", type_filter="mistake", limit=5)` to find mistakes related to what you're about to do.
2. Read the returned entries carefully. Each documents a real failure, its root cause, and the correct pattern.
3. Actively avoid repeating any matched pattern during your implementation.

**Example queries by task type:**
- Building a new API endpoint: `memory_search("backend api persist wiring", type_filter="mistake")`
- Writing Python modules: `memory_search("python import mutable", type_filter="mistake")`
- Creating dashboard UI: `memory_search("modal UI design system", type_filter="mistake")`
- Running an evolution: `memory_search("evolution scope deploy verify", type_filter="mistake")`
- Adding a new capability: `memory_search("redundancy audit existing tools", type_filter="mistake")`

**After encountering or fixing a new mistake**, save it for future reference:
```
memory_save(
    content="M-XX <CATEGORY>: <imperative rule>. <explanation of what went wrong and the correct pattern>",
    type="mistake",
    tags="<comma-separated categories like: python,import,bug or ui,modal,ux>"
)
```
