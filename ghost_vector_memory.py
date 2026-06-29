"""
ghost_vector_memory.py - Semantic memory with vector embeddings

Enables intelligent memory retrieval using semantic similarity.
Finds related memories even when using different words or phrasing.
"""

import os
import json
import sqlite3
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
from dataclasses import dataclass, asdict

log = logging.getLogger("quinely.vector_memory")

try:
    import numpy as _np
except Exception:  # numpy should be present, but degrade gracefully
    _np = None


@dataclass
class MemoryEntry:
    """A memory with semantic embedding."""
    id: str
    content: str
    summary: str
    embedding: List[float]
    metadata: Dict[str, Any]
    created_at: str
    memory_type: str
    tags: List[str]
    source: str


class SimpleEmbedding:
    """
    Simple embedding using sentence statistics.
    Not as good as OpenAI embeddings but works offline and is fast.
    """
    
    def __init__(self):
        # Simple vocabulary for basic semantic matching
        self.stopwords = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 
                         'be', 'been', 'being', 'have', 'has', 'had', 
                         'do', 'does', 'did', 'will', 'would', 'could',
                         'should', 'may', 'might', 'must', 'shall', 'can',
                         'need', 'dare', 'ought', 'used', 'to', 'of', 'in',
                         'for', 'on', 'with', 'at', 'by', 'from', 'as',
                         'into', 'through', 'during', 'before', 'after',
                         'above', 'below', 'between', 'under', 'and', 'but',
                         'or', 'yet', 'so', 'if', 'because', 'although',
                         'though', 'while', 'where', 'when', 'that', 'which',
                         'who', 'whom', 'whose', 'what', 'this', 'these',
                         'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they',
                         'me', 'him', 'her', 'us', 'them', 'my', 'your', 'his',
                         'its', 'our', 'their', 'mine', 'yours', 'hers', 'ours',
                         'theirs', 'myself', 'yourself', 'himself', 'herself',
                         'itself', 'ourselves', 'yourselves', 'themselves'}
    
    def embed(self, text: str) -> List[float]:
        """
        Create a simple embedding from text.
        Uses word frequency + n-grams for basic semantic capture.
        """
        # Normalize
        text = text.lower()
        
        # Extract words
        words = []
        current = []
        for char in text:
            if char.isalnum():
                current.append(char)
            else:
                if current:
                    word = ''.join(current)
                    if word not in self.stopwords and len(word) > 2:
                        words.append(word)
                    current = []
        if current:
            word = ''.join(current)
            if word not in self.stopwords and len(word) > 2:
                words.append(word)
        
        # Create bigrams
        bigrams = []
        for i in range(len(words) - 1):
            bigrams.append(f"{words[i]}_{words[i+1]}")
        
        # Build feature vector (simple bag of words + bigrams)
        all_features = words + bigrams
        
        # Create a hash-based embedding (deterministic)
        embedding = [0.0] * 128
        for feature in all_features:
            # Hash feature to get index
            h = hashlib.md5(feature.encode()).hexdigest()
            idx = int(h[:8], 16) % 128
            # Weight by frequency
            weight = all_features.count(feature) / len(all_features) if all_features else 0
            embedding[idx] += weight
        
        # Normalize
        magnitude = sum(x**2 for x in embedding) ** 0.5
        if magnitude > 0:
            embedding = [x / magnitude for x in embedding]
        
        return embedding


class VectorMemoryStore:
    """SQLite-backed vector memory store."""
    
    def __init__(self, db_path: Optional[str] = None):
        _default = str(Path.home() / ".ghost" / "vector_memory.db")
        self.db_path = str(db_path) if db_path else _default
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # Shared neural embedder (falls back to hashing automatically).
        try:
            from ghost_embeddings import get_embedder
            self.embedder = get_embedder()
        except Exception:
            self.embedder = SimpleEmbedding()
        self._lock = threading.RLock()
        self._cache_dirty = True          # ANN matrix needs (re)building
        self._cache = None                # dict per vector-space -> numpy matrix
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    summary TEXT,
                    embedding TEXT,  -- JSON array
                    metadata TEXT,  -- JSON
                    created_at TEXT,
                    memory_type TEXT,
                    tags TEXT,  -- JSON array
                    source TEXT
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_type 
                ON memories(memory_type)
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_created 
                ON memories(created_at)
            """)

            # Migration: track which embedding space each row belongs to so we
            # can mix legacy (hash-128) and neural vectors without corrupting
            # similarity math. NULL is treated as the legacy hash space.
            cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
            if "model" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN model TEXT")

            conn.commit()

    def _embedder_model_id(self) -> str:
        return getattr(self.embedder, "model_id", f"hash-{getattr(self.embedder, 'DIM', 128)}")

    def embed(self, text: str) -> List[float]:
        return self.embedder.embed(text)
    
    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        dot = sum(x*y for x, y in zip(a, b))
        mag_a = sum(x**2 for x in a) ** 0.5
        mag_b = sum(x**2 for x in b) ** 0.5
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)
    
    def save(self, entry: MemoryEntry, model: Optional[str] = None) -> str:
        """Save a memory entry. ``model`` records the embedding space used
        (defaults to the current embedder's model id)."""
        model = model or self._embedder_model_id()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO memories 
                (id, content, summary, embedding, metadata, created_at, memory_type, tags, source, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id,
                entry.content,
                entry.summary,
                json.dumps(entry.embedding),
                json.dumps(entry.metadata),
                entry.created_at,
                entry.memory_type,
                json.dumps(entry.tags),
                entry.source,
                model,
            ))
            conn.commit()
        self._cache_dirty = True
        return entry.id
    
    def _refresh_cache(self):
        """Load all rows into in-memory numpy matrices, grouped by embedding
        space (model id). This is the ANN index: vectorized cosine over a small
        number of dense matrices is sub-millisecond for thousands of vectors."""
        rows_meta: List[Dict[str, Any]] = []
        spaces: Dict[str, Dict[str, Any]] = {}
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT id, content, summary, embedding, metadata, created_at, "
                "memory_type, tags, source, model FROM memories"
            )
            for row in cursor.fetchall():
                emb = json.loads(row[3]) if row[3] else []
                if not emb:
                    continue
                model = row[9] or ("hash-128" if len(emb) == 128 else f"dim-{len(emb)}")
                idx = len(rows_meta)
                rows_meta.append({
                    "id": row[0], "content": row[1], "summary": row[2],
                    "metadata": json.loads(row[4]) if row[4] else {},
                    "created_at": row[5], "memory_type": row[6],
                    "tags": json.loads(row[7]) if row[7] else [],
                    "source": row[8], "model": model,
                })
                spaces.setdefault(model, {"idx": [], "vecs": []})
                spaces[model]["idx"].append(idx)
                spaces[model]["vecs"].append(emb)

        for model, sp in spaces.items():
            if _np is not None:
                try:
                    mat = _np.asarray(sp["vecs"], dtype=_np.float32)
                    norms = _np.linalg.norm(mat, axis=1, keepdims=True)
                    norms[norms == 0] = 1.0
                    sp["matrix"] = mat / norms
                except Exception:
                    sp["matrix"] = None
            else:
                sp["matrix"] = None

        self._cache = {"rows": rows_meta, "spaces": spaces}
        self._cache_dirty = False

    def _query_vec_for_space(self, model: str, query: str,
                             neural_vec, hash_vec):
        """Return the appropriate query vector for a stored space."""
        if model == self._embedder_model_id() and getattr(self.embedder, "is_neural", False):
            return neural_vec
        if model.startswith("hash-") or model == "dim-128":
            return hash_vec
        # Same-dim neural space from a different model: compare anyway if dims match.
        if neural_vec is not None and len(neural_vec) and len(neural_vec) != 128:
            return neural_vec
        return None

    def search(self, query: str, top_k: int = 5, 
               memory_type: Optional[str] = None,
               tags: Optional[List[str]] = None) -> List[Tuple[MemoryEntry, float]]:
        """
        Search memories by semantic similarity.
        Returns list of (memory, similarity_score) tuples sorted by relevance.
        """
        with self._lock:
            if self._cache_dirty or self._cache is None:
                self._refresh_cache()
            cache = self._cache

        if not cache or not cache["rows"]:
            return []

        neural_vec = self.embedder.embed(query)
        try:
            hash_vec = self.embedder.hash_embed(query)
        except Exception:
            hash_vec = SimpleEmbedding().embed(query)

        scored: List[Tuple[int, float]] = []  # (row index, score)
        for model, sp in cache["spaces"].items():
            qv = self._query_vec_for_space(model, query, neural_vec, hash_vec)
            if qv is None:
                continue
            if _np is not None and sp.get("matrix") is not None:
                q = _np.asarray(qv, dtype=_np.float32)
                if q.shape[0] != sp["matrix"].shape[1]:
                    continue
                qn = q / (_np.linalg.norm(q) or 1.0)
                sims = sp["matrix"] @ qn
                for local_i, sim in enumerate(sims):
                    scored.append((sp["idx"][local_i], float(sim)))
            else:
                for local_i, vec in enumerate(sp["vecs"]):
                    if len(vec) != len(qv):
                        continue
                    sim = self._cosine_similarity(qv, vec)
                    scored.append((sp["idx"][local_i], sim))

        rows = cache["rows"]
        results: List[Tuple[MemoryEntry, float]] = []
        for idx, sim in scored:
            if sim <= 0.1:
                continue
            r = rows[idx]
            if memory_type and r["memory_type"] != memory_type:
                continue
            if tags and not any(t in r["tags"] for t in tags):
                continue
            results.append((MemoryEntry(
                id=r["id"], content=r["content"], summary=r["summary"],
                embedding=[], metadata=r["metadata"], created_at=r["created_at"],
                memory_type=r["memory_type"], tags=r["tags"], source=r["source"],
            ), sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]
    
    def get_by_id(self, memory_id: str) -> Optional[MemoryEntry]:
        """Get a specific memory by ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            )
            row = cursor.fetchone()
            
            if row:
                return MemoryEntry(
                    id=row[0],
                    content=row[1],
                    summary=row[2],
                    embedding=json.loads(row[3]) if row[3] else [],
                    metadata=json.loads(row[4]) if row[4] else {},
                    created_at=row[5],
                    memory_type=row[6],
                    tags=json.loads(row[7]) if row[7] else [],
                    source=row[8]
                )
            return None
    
    def delete(self, memory_id: str) -> bool:
        """Delete a memory by ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE id = ?", (memory_id,)
            )
            conn.commit()
        self._cache_dirty = True
        return cursor.rowcount > 0

    def add_text(self, content: str, summary: str = "", memory_type: str = "note",
                 tags: Optional[List[str]] = None, source: str = "user",
                 metadata: Optional[Dict[str, Any]] = None,
                 entry_id: Optional[str] = None) -> str:
        """Embed and store a piece of text in one call (uses current embedder)."""
        mem_id = entry_id or hashlib.sha256(
            f"{content}{datetime.now().isoformat()}".encode()).hexdigest()[:16]
        entry = MemoryEntry(
            id=mem_id, content=content, summary=summary or content[:200],
            embedding=self.embedder.embed(content), metadata=metadata or {},
            created_at=datetime.now().isoformat(), memory_type=memory_type,
            tags=tags or [], source=source,
        )
        return self.save(entry)

    def sync_from_memory(self, memory_db=None, limit: int = 20000,
                         batch: int = 256) -> Dict[str, int]:
        """Index memories from Ghost's keyword store (ghost_memory / memory.db)
        into the semantic vector store so semantic recall works over the full
        long-term memory. Idempotent: rows already present (by id) are skipped.

        Returns {"indexed": N, "skipped": M, "total": T}.
        """
        try:
            if memory_db is None:
                from ghost_memory import MemoryDB
                memory_db = MemoryDB()
            rows = memory_db.get_recent(limit=limit)
        except Exception as e:
            log.debug("sync_from_memory: cannot read memory db: %s", e)
            return {"indexed": 0, "skipped": 0, "total": 0}

        with sqlite3.connect(self.db_path) as conn:
            existing = {r[0] for r in conn.execute("SELECT id FROM memories")}

        model_id = self._embedder_model_id()
        to_add = []
        for m in rows:
            mid = f"mem-{m.get('id')}"
            if mid in existing:
                continue
            content = (m.get("content") or "").strip()
            if not content:
                continue
            to_add.append((mid, m, content))

        indexed = 0
        for i in range(0, len(to_add), batch):
            chunk = to_add[i:i + batch]
            try:
                vecs = self.embedder.embed_batch([c[2] for c in chunk])
            except Exception as e:
                log.debug("sync_from_memory batch embed failed: %s", e)
                continue
            with sqlite3.connect(self.db_path) as conn:
                for (mid, m, content), vec in zip(chunk, vecs):
                    tags = m.get("tags") or ""
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if isinstance(tags, str) else (tags or [])
                    conn.execute("""
                        INSERT OR REPLACE INTO memories
                        (id, content, summary, embedding, metadata, created_at, memory_type, tags, source, model)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        mid, content, content[:200], json.dumps(vec), "{}",
                        m.get("timestamp", ""), m.get("type", "memory"),
                        json.dumps(tag_list), "memory_db", model_id,
                    ))
                    indexed += 1
                conn.commit()

        if indexed:
            self._cache_dirty = True
        return {"indexed": indexed, "skipped": len(rows) - indexed,
                "total": len(rows)}

    def reembed_stale(self, batch: int = 256) -> int:
        """Re-embed rows whose stored embedding space differs from the current
        embedder (e.g. legacy hash vectors after enabling neural). Returns count."""
        model_id = self._embedder_model_id()
        if not getattr(self.embedder, "is_neural", False):
            return 0
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, content FROM memories WHERE model IS NULL OR model != ?",
                (model_id,)
            ).fetchall()
        if not rows:
            return 0
        done = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            try:
                vecs = self.embedder.embed_batch([c[1] or "" for c in chunk])
            except Exception:
                break
            with sqlite3.connect(self.db_path) as conn:
                for (rid, _content), vec in zip(chunk, vecs):
                    conn.execute("UPDATE memories SET embedding = ?, model = ? WHERE id = ?",
                                 (json.dumps(vec), model_id, rid))
                    done += 1
                conn.commit()
        if done:
            self._cache_dirty = True
        return done
    
    def list_all(self, memory_type: Optional[str] = None,
                 limit: int = 100) -> List[MemoryEntry]:
        """List all memories, optionally filtered by type."""
        with sqlite3.connect(self.db_path) as conn:
            if memory_type:
                cursor = conn.execute(
                    "SELECT * FROM memories WHERE memory_type = ? ORDER BY created_at DESC LIMIT ?",
                    (memory_type, limit)
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )
            
            rows = cursor.fetchall()
            
            return [
                MemoryEntry(
                    id=row[0],
                    content=row[1],
                    summary=row[2],
                    embedding=json.loads(row[3]) if row[3] else [],
                    metadata=json.loads(row[4]) if row[4] else {},
                    created_at=row[5],
                    memory_type=row[6],
                    tags=json.loads(row[7]) if row[7] else [],
                    source=row[8]
                )
                for row in rows
            ]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the memory store."""
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            
            type_counts = {}
            cursor = conn.execute(
                "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
            )
            for row in cursor:
                type_counts[row[0]] = row[1]
            
            return {
                "total_memories": total,
                "by_type": type_counts,
                "db_path": self.db_path
            }


# Global store instance
_store: Optional[VectorMemoryStore] = None


def get_store() -> VectorMemoryStore:
    """Get or create global vector memory store."""
    global _store
    if _store is None:
        _store = VectorMemoryStore()
    return _store


def make_semantic_memory_save():
    """Create the semantic_memory_save tool."""
    
    def execute(content: str, summary: str = "", memory_type: str = "note",
                tags: List[str] = None, source: str = "user", metadata: Dict = None):
        """
        Save a memory with semantic embedding for intelligent retrieval.
        
        Args:
            content: The full content to remember
            summary: Brief summary for quick reference
            memory_type: Type of memory (note, fact, preference, code, etc.)
            tags: List of tags for categorization
            source: Where this memory came from
            metadata: Additional structured data
            
        Returns:
            Dict with memory_id and confirmation
        """
        store = get_store()
        
        # Generate ID
        memory_id = hashlib.sha256(
            f"{content}{datetime.now().isoformat()}".encode()
        ).hexdigest()[:16]
        
        # Generate embedding with the shared (neural) embedder
        embedding = store.embedder.embed(content)
        
        # Create entry
        entry = MemoryEntry(
            id=memory_id,
            content=content,
            summary=summary or content[:200],
            embedding=embedding,
            metadata=metadata or {},
            created_at=datetime.now().isoformat(),
            memory_type=memory_type,
            tags=tags or [],
            source=source
        )
        
        # Save
        store.save(entry)
        
        return {
            "memory_id": memory_id,
            "saved": True,
            "type": memory_type,
            "embedding_dimensions": len(embedding),
            "message": f"Memory saved with ID: {memory_id}"
        }
    
    return {
        "name": "semantic_memory_save",
        "description": "Save a memory with semantic embedding for intelligent, concept-based retrieval later.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Full content to remember"},
                "summary": {"type": "string", "description": "Brief summary for quick reference"},
                "memory_type": {"type": "string", "default": "note", "description": "Type: note, fact, preference, code, insight, etc."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                "source": {"type": "string", "default": "user", "description": "Source of the memory"},
                "metadata": {"type": "object", "description": "Additional structured data"}
            },
            "required": ["content"]
        },
        "execute": execute
    }


def build_vector_memory_tools(memory_db=None):
    """Build vector memory tools for the ghost tool registry."""
    return [make_semantic_memory_save(), make_semantic_memory_search(), make_hybrid_memory_search()]


def make_semantic_memory_search():
    """Create the semantic_memory_search tool."""
    
    def execute(query: str, top_k: int = 5, memory_type: str = None, tags: List[str] = None):
        """
        Search memories by semantic similarity.
        Finds conceptually related memories even with different words.
        
        Args:
            query: Natural language query describing what you're looking for
            top_k: Number of results to return
            memory_type: Filter by memory type
            tags: Filter by tags
            
        Returns:
            List of matching memories with similarity scores
        """
        store = get_store()
        results = store.search(query, top_k, memory_type, tags)
        
        return {
            "query": query,
            "results": [
                {
                    "id": entry.id,
                    "content": entry.content[:500] + "..." if len(entry.content) > 500 else entry.content,
                    "summary": entry.summary,
                    "type": entry.memory_type,
                    "tags": entry.tags,
                    "similarity_score": round(score, 3),
                    "created_at": entry.created_at
                }
                for entry, score in results
            ],
            "count": len(results)
        }
    
    return {
        "name": "semantic_memory_search",
        "description": "Search memories by meaning/semantics. Finds related memories even when phrased differently.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "top_k": {"type": "integer", "default": 5, "description": "Number of results"},
                "memory_type": {"type": "string", "description": "Filter by memory type"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags"}
            },
            "required": ["query"]
        },
        "execute": execute
    }


def make_hybrid_memory_search():
    """Create the hybrid_memory_search tool combining text + semantic."""
    
    def execute(query: str, top_k: int = 5):
        """
        Search using both text matching and semantic similarity.
        Best of both worlds: finds exact matches and conceptually similar content.
        
        Args:
            query: Search query
            top_k: Number of results
            
        Returns:
            Combined results with relevance scores
        """
        from ghost_memory import memory_search
        
        # Get semantic results
        store = get_store()
        semantic_results = store.search(query, top_k * 2)
        semantic_ids = {entry.id for entry, _ in semantic_results}
        
        # Get text results from existing memory system
        try:
            text_results = memory_search(query, limit=top_k * 2)
        except Exception:
            text_results = []
        
        # Combine and deduplicate
        seen = set()
        combined = []
        
        # Add semantic results first (weighted higher)
        for entry, score in semantic_results:
            if entry.id not in seen:
                seen.add(entry.id)
                combined.append({
                    "id": entry.id,
                    "content": entry.content[:500] + "..." if len(entry.content) > 500 else entry.content,
                    "summary": entry.summary,
                    "type": entry.memory_type,
                    "tags": entry.tags,
                    "relevance": round(score * 0.6 + 0.4, 3),  # Weighted boost
                    "match_type": "semantic",
                    "created_at": entry.created_at
                })
        
        # Add text results
        for mem in text_results:
            mem_id = mem.get("id", hashlib.sha256(mem.get("content", "").encode()).hexdigest()[:16])
            if mem_id not in seen:
                seen.add(mem_id)
                combined.append({
                    "id": mem_id,
                    "content": mem.get("content", "")[:500] + "..." if len(mem.get("content", "")) > 500 else mem.get("content", ""),
                    "summary": mem.get("summary", ""),
                    "type": mem.get("type", "unknown"),
                    "tags": mem.get("tags", []),
                    "relevance": 0.5,  # Base score for text matches
                    "match_type": "text",
                    "created_at": mem.get("created_at", "")
                })
        
        # Sort by relevance
        combined.sort(key=lambda x: x["relevance"], reverse=True)
        
        return {
            "query": query,
            "results": combined[:top_k],
            "semantic_matches": len(semantic_results),
            "text_matches": len(text_results),
            "count": len(combined[:top_k])
        }
    
    return {
        "name": "hybrid_memory_search",
        "description": "Search memories using both semantic similarity and text matching. Best comprehensive search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "top_k": {"type": "integer", "default": 5, "description": "Number of results"}
            },
            "required": ["query"]
        },
        "execute": execute
    }
