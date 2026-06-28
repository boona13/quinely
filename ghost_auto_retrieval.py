"""
ghost_auto_retrieval.py — Automatic pre-turn memory retrieval (RAG).

Modern agents don't wait for the model to *decide* to look something up — they
retrieve relevant long-term context automatically before each turn and inject it
into the prompt. Ghost previously only recalled memory when the LLM chose to call
``memory_search``. This module closes that gap.

``retrieve_context_block(query)`` fuses three retrieval signals over Ghost's
existing stores (no schema changes, so the live memory DBs are untouched):

  1. Keyword / full-text recall  (daemon MemoryDB.search, SQLite FTS)
  2. Semantic similarity         (ghost_vector_memory vector store)
  3. A lightweight **reranker** that blends the source score with query-term
     overlap and recency, then dedupes and trims to a token budget.

It is defensive by construction: every backend is wrapped in try/except, short
or greeting-like queries are skipped, and any failure yields an empty string so
a retrieval problem can never break a chat turn.
"""

import logging
import math
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

log = logging.getLogger("ghost.retrieval")

_GREETINGS = {
    "hi", "hello", "hey", "yo", "sup", "hiya", "howdy", "thanks", "thank you",
    "ok", "okay", "cool", "nice", "lol", "good morning", "good evening",
    "good night", "gm", "gn", "ping", "test",
}
_WORD_RE = re.compile(r"[a-zA-Z0-9]{3,}")
_STOP = {
    "the", "and", "for", "are", "was", "you", "your", "what", "with", "this",
    "that", "have", "has", "had", "can", "could", "would", "should", "about",
    "from", "into", "how", "why", "who", "which", "does", "did", "but", "not",
}


def _terms(text: str) -> set:
    return {w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOP}


def _recency_bonus(created_at: str) -> float:
    """Small boost for recent memories: ~+0.15 today decaying over ~60 days."""
    if not created_at:
        return 0.0
    try:
        s = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
        return 0.15 * math.exp(-age_days / 60.0)
    except Exception:
        return 0.0


def _norm_key(content: str) -> str:
    return re.sub(r"\s+", " ", (content or "").strip().lower())[:160]


def _gather_keyword(query: str, limit: int, daemon=None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        # Use the live daemon's FTS store. (There is no module-level
        # ``memory_search`` in ghost_memory — that's a tool *factory* — so the
        # old top-level import silently disabled this leg entirely.)
        db = getattr(daemon, "memory_db", None) if daemon is not None else None
        if db is None:
            from ghost_memory import MemoryDB
            db = MemoryDB()
        for m in (db.search(query, limit=limit) or []):
            if not isinstance(m, dict):
                continue
            content = m.get("content") or m.get("source_preview") or m.get("summary") or ""
            if content:
                out.append({
                    "content": content,
                    "base": 0.45,
                    "created": m.get("timestamp") or m.get("created_at", ""),
                    "src": "keyword",
                })
    except Exception as e:
        log.debug("keyword retrieval failed: %s", e)
    return out


def _gather_semantic(query: str, limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        from ghost_vector_memory import get_store
        for entry, sim in get_store().search(query, top_k=limit):
            content = entry.summary or entry.content
            if content:
                out.append({
                    "content": content,
                    "base": 0.30 + 0.5 * float(sim),  # map similarity into score
                    "created": entry.created_at,
                    "src": "semantic",
                })
    except Exception as e:
        log.debug("semantic retrieval failed: %s", e)
    return out


def rerank(query: str, candidates: List[Dict[str, Any]],
           max_items: int) -> List[Dict[str, Any]]:
    """Blend base score + query-term overlap + recency; dedupe; sort."""
    q_terms = _terms(query)
    best: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        content = c.get("content", "")
        if not content:
            continue
        overlap = 0.0
        if q_terms:
            c_terms = _terms(content)
            if c_terms:
                overlap = len(q_terms & c_terms) / len(q_terms)
        score = c.get("base", 0.0) + 0.4 * overlap + _recency_bonus(c.get("created", ""))
        key = _norm_key(content)
        prev = best.get(key)
        if prev is None or score > prev["score"]:
            best[key] = {"content": content, "score": score, "src": c.get("src", "")}
    ranked = sorted(best.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:max_items]


def retrieve_context_block(query: str, daemon=None, max_items: int = 5,
                           max_chars: int = 1500, min_query_len: int = 12,
                           min_score: float = 0.25) -> str:
    """Return a formatted, token-budgeted memory block for ``query`` (or "")."""
    q = (query or "").strip()
    if len(q) < min_query_len:
        return ""
    if q.lower().strip("!?. ") in _GREETINGS:
        return ""

    candidates = _gather_keyword(q, max_items * 2, daemon=daemon) + _gather_semantic(q, max_items * 2)
    if not candidates:
        return ""

    ranked = [r for r in rerank(q, candidates, max_items) if r["score"] >= min_score]
    if not ranked:
        return ""

    lines = [
        "## Relevant memory (auto-retrieved)",
        "Background from Ghost's long-term memory that may help. Use it only if "
        "relevant to the user's message; ignore otherwise.",
    ]
    used = 0
    shown = 0
    for r in ranked:
        snippet = re.sub(r"\s+", " ", r["content"]).strip()
        if len(snippet) > 320:
            snippet = snippet[:320] + "…"
        line = f"- {snippet}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
        shown += 1
    if shown == 0:
        return ""
    return "\n".join(lines)
