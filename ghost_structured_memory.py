"""
Ghost Structured Memory — LLM-powered persistent memory with confidence-scored facts.

Mirrored from DeerFlow's memory system (agents/memory/).

Architecture:
  - Dual representation: narrative summaries + structured facts
  - LLM-as-memory-manager with per-section shouldUpdate gates
  - Temporal decay via history tiers (recent → earlier → long-term)
  - Confidence-gated fact storage with max-facts eviction
  - Token-budgeted injection into system prompts
  - Atomic file writes with mtime-based cache invalidation
  - Debounced update queue to avoid redundant LLM calls
"""

import copy
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("quinely.structured_memory")

GHOST_HOME = Path.home() / ".ghost"
STRUCTURED_MEMORY_FILE = GHOST_HOME / "structured_memory.json"


# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

@dataclass
class StructuredMemoryConfig:
    enabled: bool = True
    debounce_seconds: int = 30
    max_facts: int = 100
    fact_confidence_threshold: float = 0.7
    max_injection_tokens: int = 2000
    storage_path: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "StructuredMemoryConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


_config = StructuredMemoryConfig()


def get_structured_memory_config() -> StructuredMemoryConfig:
    return _config


def set_structured_memory_config(cfg: StructuredMemoryConfig):
    global _config
    _config = cfg


# ═══════════════════════════════════════════════════════════════════
#  DATA SCHEMA  (mirrors DeerFlow's memory.json structure)
# ═══════════════════════════════════════════════════════════════════

def _create_empty_memory() -> dict[str, Any]:
    return {
        "version": "1.0",
        "lastUpdated": datetime.utcnow().isoformat() + "Z",
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentMonths": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": [],
    }


# ═══════════════════════════════════════════════════════════════════
#  FILE I/O WITH MTIME-BASED CACHE  (mirrors DeerFlow's updater.py)
# ═══════════════════════════════════════════════════════════════════

_memory_cache: tuple[dict[str, Any], float | None] | None = None


def _get_memory_file_path() -> Path:
    cfg = get_structured_memory_config()
    if cfg.storage_path:
        p = Path(cfg.storage_path)
        return p if p.is_absolute() else GHOST_HOME / p
    return STRUCTURED_MEMORY_FILE


def get_memory_data() -> dict[str, Any]:
    """Get current memory data (cached with file mtime check)."""
    global _memory_cache
    file_path = _get_memory_file_path()

    try:
        current_mtime = file_path.stat().st_mtime if file_path.exists() else None
    except OSError:
        current_mtime = None

    if _memory_cache is None or _memory_cache[1] != current_mtime:
        memory_data = _load_memory_from_file()
        _memory_cache = (memory_data, current_mtime)
        return memory_data

    return _memory_cache[0]


def reload_memory_data() -> dict[str, Any]:
    """Force-reload memory data from disk."""
    global _memory_cache
    memory_data = _load_memory_from_file()
    file_path = _get_memory_file_path()
    try:
        mtime = file_path.stat().st_mtime if file_path.exists() else None
    except OSError:
        mtime = None
    _memory_cache = (memory_data, mtime)
    return memory_data


def _load_memory_from_file() -> dict[str, Any]:
    file_path = _get_memory_file_path()
    if not file_path.exists():
        return _create_empty_memory()
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load structured memory: %s", e)
        return _create_empty_memory()


_UPLOAD_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:"
    r"upload(?:ed|ing)?(?:\s+\w+){0,3}\s+(?:file|files?|document|documents?|attachment|attachments?)"
    r"|file\s+upload"
    r"|/mnt/user-data/uploads/"
    r"|<uploaded_files>"
    r")[^.!?]*[.!?]?\s*",
    re.IGNORECASE,
)


def _robust_json_parse(raw: str) -> dict | None:
    """Parse JSON from an LLM response, tolerating common formatting issues.

    Strategies (tried in order):
      1. Direct json.loads
      2. Strip markdown fences and retry
      3. Extract the outermost {...} block
      4. Fix trailing commas and retry
      5. Try ast.literal_eval as last resort
    Returns parsed dict or None.
    """
    text = raw.strip()

    # Strategy 1: direct parse
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        text = "\n".join(lines[1:end]).strip()
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: extract outermost { ... } block (handles preamble/postamble text)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidate = text[first:last + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 4: fix trailing commas before } or ]
        fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            result = json.loads(fixed)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 5: fix unescaped newlines inside string values
        fixed2 = re.sub(r'(?<=": ")(.*?)(?=")', lambda m: m.group(0).replace("\n", "\\n"), fixed)
        try:
            result = json.loads(fixed2)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    log.debug("All JSON parse strategies failed for response (len=%d)", len(raw))
    return None


def _strip_upload_mentions(memory_data: dict[str, Any]) -> dict[str, Any]:
    """Remove sentences about file uploads from all memory summaries and facts."""
    for section in ("user", "history"):
        section_data = memory_data.get(section)
        if not isinstance(section_data, dict):
            continue
        for _key, val in section_data.items():
            if isinstance(val, dict) and isinstance(val.get("summary"), str):
                cleaned = _UPLOAD_SENTENCE_RE.sub("", val["summary"]).strip()
                cleaned = re.sub(r"  +", " ", cleaned)
                val["summary"] = cleaned
    facts = memory_data.get("facts", [])
    if facts:
        memory_data["facts"] = [
            f for f in facts
            if isinstance(f, dict) and not _UPLOAD_SENTENCE_RE.search(f.get("content") or "")
        ]
    return memory_data


def _save_memory_to_file(memory_data: dict[str, Any]) -> bool:
    """Atomic write: temp file + rename, then update cache."""
    global _memory_cache
    file_path = _get_memory_file_path()

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        memory_data["lastUpdated"] = datetime.utcnow().isoformat() + "Z"

        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(memory_data, f, indent=2, ensure_ascii=False)
        temp_path.replace(file_path)

        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            mtime = None
        _memory_cache = (memory_data, mtime)

        log.info("Structured memory saved to %s", file_path)
        return True
    except OSError as e:
        log.error("Failed to save structured memory: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════
#  LLM PROMPTS  (mirrors DeerFlow's memory/prompt.py)
# ═══════════════════════════════════════════════════════════════════

MEMORY_UPDATE_PROMPT = """You are a memory management system. Your task is to analyze a conversation and update the user's memory profile.

Current Memory State:
<current_memory>
{current_memory}
</current_memory>

New Conversation to Process:
<conversation>
{conversation}
</conversation>

Instructions:
1. Analyze the conversation for important information about the user
2. Extract relevant facts, preferences, and context with specific details (numbers, names, technologies)
3. Update the memory sections as needed following the detailed length guidelines below

Memory Section Guidelines:

**User Context** (Current state - concise summaries):
- workContext: Professional role, company, key projects, main technologies (2-3 sentences)
- personalContext: Languages, communication preferences, key interests (1-2 sentences)
- topOfMind: Multiple ongoing focus areas and priorities (3-5 sentences, detailed paragraph)
  Captures SEVERAL concurrent focus areas, not just one task

**History** (Temporal context - rich paragraphs):
- recentMonths: Detailed summary of recent activities (4-6 sentences). Timeline: Last 1-3 months
- earlierContext: Important historical patterns (3-5 sentences). Timeline: 3-12 months ago
- longTermBackground: Persistent background and foundational context (2-4 sentences)

**Facts Extraction**:
- Extract specific, quantifiable details
- Categories: preference, knowledge, context, behavior, goal
- Confidence levels:
  * 0.9-1.0: Explicitly stated facts
  * 0.7-0.8: Strongly implied from actions/discussions
  * 0.5-0.6: Inferred patterns (use sparingly)

Output Format (JSON):
{{
  "user": {{
    "workContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "personalContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "topOfMind": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "history": {{
    "recentMonths": {{ "summary": "...", "shouldUpdate": true/false }},
    "earlierContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "longTermBackground": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "newFacts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal", "confidence": 0.0-1.0 }}
  ],
  "factsToRemove": ["fact_id_1", "fact_id_2"]
}}

Important Rules:
- Only set shouldUpdate=true if there's meaningful new information
- Only add facts that are clearly stated (0.9+) or strongly implied (0.7+)
- Remove facts that are contradicted by new information
- Do NOT record file upload events in memory
- Return ONLY valid JSON, no explanation or markdown."""


def _count_tokens(text: str) -> int:
    """Count tokens using tiktoken if available, else estimate."""
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return len(text) // 4


def format_memory_for_injection(memory_data: dict[str, Any] | None = None,
                                 max_tokens: int | None = None) -> str:
    """Format memory data for injection into Ghost's system prompt.

    Mirrors DeerFlow's format_memory_for_injection — token-budgeted,
    only includes summaries (not raw facts) to save context window space.
    """
    if memory_data is None:
        memory_data = get_memory_data()
    if max_tokens is None:
        max_tokens = get_structured_memory_config().max_injection_tokens

    if not memory_data:
        return ""

    def _get_summary(container, key):
        """Safely extract a string summary from nested dicts."""
        if not isinstance(container, dict):
            return ""
        val = container.get(key)
        if not isinstance(val, dict):
            return ""
        s = val.get("summary")
        return s if isinstance(s, str) and s.strip() else ""

    sections = []

    user_data = memory_data.get("user")
    if isinstance(user_data, dict):
        user_sections = []
        s = _get_summary(user_data, "workContext")
        if s:
            user_sections.append(f"Work: {s}")
        s = _get_summary(user_data, "personalContext")
        if s:
            user_sections.append(f"Personal: {s}")
        s = _get_summary(user_data, "topOfMind")
        if s:
            user_sections.append(f"Current Focus: {s}")
        if user_sections:
            sections.append("User Context:\n" + "\n".join(f"- {s}" for s in user_sections))

    history_data = memory_data.get("history")
    if isinstance(history_data, dict):
        history_sections = []
        s = _get_summary(history_data, "recentMonths")
        if s:
            history_sections.append(f"Recent: {s}")
        s = _get_summary(history_data, "earlierContext")
        if s:
            history_sections.append(f"Earlier: {s}")
        if history_sections:
            sections.append("History:\n" + "\n".join(f"- {s}" for s in history_sections))

    def _safe_confidence(f):
        try:
            return float(f.get("confidence", 0))
        except (TypeError, ValueError):
            return 0.0

    facts = [f for f in memory_data.get("facts", []) if isinstance(f, dict)]
    if facts:
        top_facts = sorted(facts, key=_safe_confidence, reverse=True)[:15]
        fact_lines = [f"- [{f.get('category', '?')}] {f['content']}" for f in top_facts if f.get("content")]
        if fact_lines:
            sections.append("Key Facts:\n" + "\n".join(fact_lines))

    if not sections:
        return ""

    result = "\n\n".join(sections)

    token_count = _count_tokens(result)
    if token_count > max_tokens:
        char_per_token = len(result) / token_count
        target_chars = int(max_tokens * char_per_token * 0.95)
        result = result[:target_chars] + "\n..."

    return result


def format_conversation_for_update(messages: list[dict]) -> str:
    """Format Ghost's message list for the memory update prompt.

    Filters out tool messages and ephemeral upload blocks,
    mirroring DeerFlow's MemoryMiddleware filtering.
    """
    lines = []
    skip_next_assistant = False

    for msg in messages:
        role = msg.get("role", "")

        if role == "tool":
            continue

        if role == "user":
            content = str(msg.get("content", ""))
            content = re.sub(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", "", content).strip()
            if not content:
                skip_next_assistant = True
                continue
            skip_next_assistant = False
            if len(content) > 1000:
                content = content[:1000] + "..."
            lines.append(f"User: {content}")

        elif role == "assistant":
            if skip_next_assistant:
                skip_next_assistant = False
                continue
            if msg.get("tool_calls"):
                continue
            content = str(msg.get("content", ""))
            if not content.strip():
                continue
            if len(content) > 1000:
                content = content[:1000] + "..."
            lines.append(f"Ghost: {content}")

    return "\n\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  MEMORY UPDATER  (mirrors DeerFlow's updater.py MemoryUpdater)
# ═══════════════════════════════════════════════════════════════════

class StructuredMemoryUpdater:
    """Updates structured memory using an LLM call."""

    def __init__(self, engine=None):
        self._engine = engine

    def update(self, messages: list[dict], session_id: str | None = None) -> bool:
        """Run the LLM to analyze conversation and update memory.

        Args:
            messages: Ghost-format message list.
            session_id: Optional session/thread ID for fact provenance.

        Returns:
            True if memory was updated successfully.
        """
        cfg = get_structured_memory_config()
        if not cfg.enabled:
            return False
        if not messages:
            return False
        if self._engine is None:
            log.warning("No LLM engine provided for memory update")
            return False

        try:
            current_memory = copy.deepcopy(get_memory_data())
            conversation_text = format_conversation_for_update(messages)

            if not conversation_text.strip():
                return False

            prompt = MEMORY_UPDATE_PROMPT.format(
                current_memory=json.dumps(current_memory, indent=2),
                conversation=conversation_text,
            )

            response = self._engine.single_shot(
                system_prompt="You are a memory management system. Return ONLY valid JSON.",
                user_message=prompt,
                temperature=0.1,
                max_tokens=2048,
            )

            if not response or not response.strip():
                return False

            update_data = _robust_json_parse(response)
            if update_data is None:
                log.warning("Failed to parse LLM memory update response after all strategies")
                return False

            updated_memory = self._apply_updates(current_memory, update_data, session_id)
            updated_memory = _strip_upload_mentions(updated_memory)

            return _save_memory_to_file(updated_memory)
        except Exception as e:
            log.error("Structured memory update failed: %s", e)
            return False

    def _apply_updates(
        self,
        current_memory: dict[str, Any],
        update_data: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply LLM-generated updates to memory. Mirrors DeerFlow's _apply_updates."""
        cfg = get_structured_memory_config()
        now = datetime.utcnow().isoformat() + "Z"

        for section_group, sections in [
            ("user", ["workContext", "personalContext", "topOfMind"]),
            ("history", ["recentMonths", "earlierContext", "longTermBackground"]),
        ]:
            group_updates = update_data.get(section_group)
            if not isinstance(group_updates, dict):
                continue
            for section in sections:
                section_data = group_updates.get(section)
                if not isinstance(section_data, dict):
                    continue
                if section_data.get("shouldUpdate") and section_data.get("summary"):
                    current_memory.setdefault(section_group, {})[section] = {
                        "summary": section_data["summary"],
                        "updatedAt": now,
                    }

        raw_removals = update_data.get("factsToRemove")
        facts_to_remove = set(raw_removals) if isinstance(raw_removals, list) else set()
        if facts_to_remove:
            current_memory["facts"] = [
                f for f in current_memory.get("facts", [])
                if isinstance(f, dict) and f.get("id") not in facts_to_remove
            ]

        new_facts = update_data.get("newFacts")
        if not isinstance(new_facts, list):
            new_facts = []
        for fact in new_facts:
            if not isinstance(fact, dict):
                continue
            try:
                confidence = float(fact.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            if confidence >= cfg.fact_confidence_threshold:
                fact_entry = {
                    "id": f"fact_{uuid.uuid4().hex[:8]}",
                    "content": fact.get("content", ""),
                    "category": fact.get("category", "context"),
                    "confidence": confidence,
                    "createdAt": now,
                    "source": session_id or "unknown",
                }
                current_memory.setdefault("facts", []).append(fact_entry)

        all_facts = [f for f in current_memory.get("facts", []) if isinstance(f, dict)]
        if len(all_facts) > cfg.max_facts:
            def _safe_conf(f):
                try:
                    return float(f.get("confidence", 0))
                except (TypeError, ValueError):
                    return 0.0
            all_facts = sorted(all_facts, key=_safe_conf, reverse=True)[:cfg.max_facts]
        current_memory["facts"] = all_facts

        return current_memory


# ═══════════════════════════════════════════════════════════════════
#  DEBOUNCED UPDATE QUEUE  (mirrors DeerFlow's memory/queue.py)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ConversationContext:
    session_id: str
    messages: list[dict]
    timestamp: datetime = field(default_factory=datetime.utcnow)


class MemoryUpdateQueue:
    """Debounced queue for structured memory updates.

    Collects conversation contexts and processes them after a configurable
    debounce period. Multiple conversations within the window are batched.
    If the same session sends multiple messages, only the latest is kept.
    """

    def __init__(self, engine=None):
        self._engine = engine
        self._queue: list[ConversationContext] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._processing = False
        self._done_event = threading.Event()
        self._done_event.set()

    def set_engine(self, engine):
        self._engine = engine

    def add(self, session_id: str, messages: list[dict]) -> None:
        """Add a conversation to the update queue."""
        cfg = get_structured_memory_config()
        if not cfg.enabled:
            return

        context = ConversationContext(session_id=session_id, messages=messages)

        with self._lock:
            self._queue = [c for c in self._queue if c.session_id != session_id]
            self._queue.append(context)
            self._reset_timer()

        log.debug("Memory update queued for session %s, queue size: %d",
                  session_id, len(self._queue))

    def _reset_timer(self) -> None:
        cfg = get_structured_memory_config()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(cfg.debounce_seconds, self._process_queue)
        self._timer.daemon = True
        self._timer.start()

    def _process_queue(self) -> None:
        with self._lock:
            if self._processing:
                self._reset_timer()
                return
            if not self._queue:
                return
            self._processing = True
            self._done_event.clear()
            contexts = self._queue.copy()
            self._queue.clear()
            self._timer = None

        log.info("Processing %d queued structured memory updates", len(contexts))

        try:
            updater = StructuredMemoryUpdater(engine=self._engine)
            for ctx in contexts:
                try:
                    success = updater.update(ctx.messages, session_id=ctx.session_id)
                    if success:
                        log.info("Structured memory updated for session %s", ctx.session_id)
                except Exception as e:
                    log.warning("Memory update failed for session %s: %s", ctx.session_id, e)
                if len(contexts) > 1:
                    time.sleep(0.5)
        finally:
            with self._lock:
                self._processing = False
            self._done_event.set()

    def flush(self) -> None:
        """Force immediate processing (for shutdown/testing).

        Blocks until any in-flight processing completes, then processes
        any remaining items synchronously.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._done_event.wait(timeout=120)
        self._process_queue()

    def clear(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._queue.clear()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def is_processing(self) -> bool:
        with self._lock:
            return self._processing


# ═══════════════════════════════════════════════════════════════════
#  SINGLETON QUEUE
# ═══════════════════════════════════════════════════════════════════

_memory_queue: MemoryUpdateQueue | None = None
_queue_lock = threading.Lock()


def get_memory_queue(engine=None) -> MemoryUpdateQueue:
    global _memory_queue
    with _queue_lock:
        if _memory_queue is None:
            _memory_queue = MemoryUpdateQueue(engine=engine)
        elif engine is not None:
            _memory_queue.set_engine(engine)
        return _memory_queue


def reset_memory_queue():
    global _memory_queue
    with _queue_lock:
        if _memory_queue is not None:
            _memory_queue.clear()
        _memory_queue = None


# ═══════════════════════════════════════════════════════════════════
#  TOOL BUILDERS  (for Ghost's tool registry)
# ═══════════════════════════════════════════════════════════════════

def build_structured_memory_tools(engine=None) -> list[dict]:
    """Build tools for Ghost to interact with structured memory."""
    return [
        _make_memory_status_tool(),
        _make_memory_query_tool(),
    ]


def _make_memory_status_tool() -> dict:
    def execute():
        data = get_memory_data()
        facts = data.get("facts", [])
        categories = {}
        for f in facts:
            cat = f.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1
        return json.dumps({
            "version": data.get("version", "1.0"),
            "lastUpdated": data.get("lastUpdated", "never"),
            "sections": {
                "workContext": bool(data.get("user", {}).get("workContext", {}).get("summary")),
                "personalContext": bool(data.get("user", {}).get("personalContext", {}).get("summary")),
                "topOfMind": bool(data.get("user", {}).get("topOfMind", {}).get("summary")),
                "recentMonths": bool(data.get("history", {}).get("recentMonths", {}).get("summary")),
                "earlierContext": bool(data.get("history", {}).get("earlierContext", {}).get("summary")),
                "longTermBackground": bool(data.get("history", {}).get("longTermBackground", {}).get("summary")),
            },
            "facts_count": len(facts),
            "facts_by_category": categories,
        }, indent=2)

    return {
        "name": "structured_memory_status",
        "description": (
            "Get the status of Ghost's structured memory system — "
            "which sections have data, how many facts are stored, breakdown by category."
        ),
        "parameters": {"type": "object", "properties": {}},
        "execute": execute,
    }


def _make_memory_query_tool() -> dict:
    def execute(section: str = "all"):
        data = get_memory_data()
        if section == "all":
            return json.dumps(data, indent=2, default=str)
        if section == "facts":
            return json.dumps(data.get("facts", []), indent=2, default=str)
        if section in ("workContext", "personalContext", "topOfMind"):
            return json.dumps(data.get("user", {}).get(section, {}), indent=2, default=str)
        if section in ("recentMonths", "earlierContext", "longTermBackground"):
            return json.dumps(data.get("history", {}).get(section, {}), indent=2, default=str)
        return json.dumps({"error": f"Unknown section: {section}"})

    return {
        "name": "structured_memory_query",
        "description": (
            "Query Ghost's structured memory. Sections: all, facts, "
            "workContext, personalContext, topOfMind, recentMonths, "
            "earlierContext, longTermBackground."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "Section to query (default: all)",
                    "default": "all",
                },
            },
        },
        "execute": execute,
    }
