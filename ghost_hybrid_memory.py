"""
ghost_hybrid_memory.py — Hybrid Memory with SQLite FTS5 + Vector Search

Combines full-text search (BM25 via FTS5) with vector embeddings (cosine similarity),
temporal decay, and MMR diversity reranking.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

PROJECT_DIR = Path(__file__).resolve().parent
GHOST_DIR = Path.home() / ".ghost"
DEFAULT_DB_PATH = GHOST_DIR / "hybrid_memory.db"
EVERGREEN_NAMES = {"MEMORY.md", "memory.md", "SOUL.md", "soul.md", "README.md"}

# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_markdown(text: str, max_chars: int = 3000, overlap_chars: int = 200) -> list[dict]:
    """Split text into overlapping chunks on line boundaries.

    Returns list of dicts with keys: start_line, end_line, text, hash.
    """
    lines = text.split("\n")
    chunks = []
    current_chars = 0
    chunk_start = 0
    chunk_lines: list[str] = []

    for i, line in enumerate(lines):
        line_len = len(line) + 1
        if current_chars + line_len > max_chars and chunk_lines:
            chunk_text = "\n".join(chunk_lines)
            chunks.append({
                "start_line": chunk_start,
                "end_line": chunk_start + len(chunk_lines) - 1,
                "text": chunk_text,
                "hash": hashlib.sha256(chunk_text.encode()).hexdigest()[:16],
            })
            overlap_target = overlap_chars
            backtrack = 0
            overlap_lines: list[str] = []
            for ln in reversed(chunk_lines):
                backtrack += len(ln) + 1
                overlap_lines.insert(0, ln)
                if backtrack >= overlap_target:
                    break
            chunk_start = i - len(overlap_lines) + 1
            chunk_lines = list(overlap_lines)
            current_chars = sum(len(ln) + 1 for ln in chunk_lines)
        chunk_lines.append(line)
        current_chars += line_len

    if chunk_lines:
        chunk_text = "\n".join(chunk_lines)
        chunks.append({
            "start_line": chunk_start,
            "end_line": chunk_start + len(chunk_lines) - 1,
            "text": chunk_text,
            "hash": hashlib.sha256(chunk_text.encode()).hexdigest()[:16],
        })

    return chunks


# ---------------------------------------------------------------------------
# Embedding Providers
# ---------------------------------------------------------------------------

class EmbeddingProvider(ABC):
    """Protocol for embedding providers."""

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class SimpleEmbeddingProvider(EmbeddingProvider):
    """Hash-based embeddings for offline / no-API-key scenarios.
    Deterministic, fast, but low semantic fidelity."""

    STOPWORDS = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "must", "shall", "can", "need", "to", "of",
        "in", "for", "on", "with", "at", "by", "from", "as", "into", "through",
        "during", "before", "after", "above", "below", "between", "under",
        "and", "but", "or", "yet", "so", "if", "because", "although", "though",
        "while", "where", "when", "that", "which", "who", "whom", "whose",
        "what", "this", "these", "those", "i", "you", "he", "she", "it", "we",
        "they", "me", "him", "her", "us", "them", "my", "your", "his", "its",
        "our", "their",
    })
    DIM = 128

    @property
    def model_name(self) -> str:
        return "simple-hash-128"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def _tokenize(self, text: str) -> list[str]:
        text = text.lower()
        tokens = re.findall(r"[a-z0-9]+", text)
        return [t for t in tokens if t not in self.STOPWORDS and len(t) > 2]

    def _embed(self, text: str) -> list[float]:
        tokens = self._tokenize(text)
        bigrams = [f"{tokens[i]}_{tokens[i+1]}" for i in range(len(tokens) - 1)]
        features = tokens + bigrams
        vec = [0.0] * self.DIM
        for feat in features:
            idx = int(hashlib.md5(feat.encode()).hexdigest()[:8], 16) % self.DIM
            vec[idx] += 1.0
        mag = math.sqrt(sum(x * x for x in vec))
        if mag > 0:
            vec = [x / mag for x in vec]
        return vec

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]


class LocalNeuralEmbeddingProvider(EmbeddingProvider):
    """Keyless local neural embeddings via the shared ghost_embeddings model
    (model2vec). High semantic fidelity, no API key, runs on CPU. Falls back to
    hashing automatically inside the shared embedder if the model is unavailable."""

    def __init__(self):
        from ghost_embeddings import get_embedder
        self._emb = get_embedder()

    @property
    def model_name(self) -> str:
        return self._emb.model_id

    @property
    def dimensions(self) -> int:
        return self._emb.dim

    @property
    def is_neural(self) -> bool:
        return getattr(self._emb, "is_neural", False)

    def embed_query(self, text: str) -> list[float]:
        return self._emb.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return self._emb.embed_batch(texts)


class OpenRouterEmbeddingProvider(EmbeddingProvider):
    """Uses OpenRouter's embedding endpoint. Falls back to SimpleEmbeddingProvider on failure."""

    DEFAULT_MODEL = "openai/text-embedding-3-small"
    DIM = 1536

    def __init__(self, api_key: str, model: str = ""):
        self._api_key = api_key
        self.MODEL = model or self.DEFAULT_MODEL
        self._fallback = SimpleEmbeddingProvider()
        self._use_fallback = False

    @property
    def model_name(self) -> str:
        return self.MODEL if not self._use_fallback else self._fallback.model_name

    @property
    def dimensions(self) -> int:
        return self.DIM if not self._use_fallback else self._fallback.dimensions

    def _call_api(self, texts: list[str]) -> list[list[float]] | None:
        if self._use_fallback:
            return None
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.MODEL, "input": texts},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            results = sorted(data["data"], key=lambda d: d["index"])
            return [r["embedding"] for r in results]
        except Exception:
            self._use_fallback = True
            return None

    def embed_query(self, text: str) -> list[float]:
        result = self._call_api([text])
        if result:
            return result[0]
        return self._fallback.embed_query(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        result = self._call_api(texts)
        if result:
            return result
        return self._fallback.embed_batch(texts)


class GeminiEmbeddingProvider(EmbeddingProvider):
    """Uses Google Gemini's embedding endpoint. Falls back on failure."""

    DEFAULT_MODEL = "text-embedding-004"
    DIM = 768

    def __init__(self, api_key: str, model: str = ""):
        self._api_key = api_key
        self.MODEL = model or self.DEFAULT_MODEL
        self._fallback = SimpleEmbeddingProvider()
        self._failed = False

    @property
    def model_name(self) -> str:
        return f"gemini/{self.MODEL}" if not self._failed else self._fallback.model_name

    @property
    def dimensions(self) -> int:
        return self.DIM if not self._failed else self._fallback.dimensions

    def _call_api(self, texts: list[str]) -> list[list[float]] | None:
        if self._failed:
            return None
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.MODEL}:batchEmbedContents?key={self._api_key}"
            )
            requests_body = [{"model": f"models/{self.MODEL}", "content": {"parts": [{"text": t}]}}
                             for t in texts]
            resp = requests.post(url, json={"requests": requests_body}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return [e["values"] for e in data.get("embeddings", [])]
        except Exception:
            self._failed = True
            return None

    def embed_query(self, text: str) -> list[float]:
        result = self._call_api([text])
        return result[0] if result else self._fallback.embed_query(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        result = self._call_api(texts)
        return result if result else self._fallback.embed_batch(texts)


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Uses Ollama's local embedding endpoint. Falls back on failure."""

    DEFAULT_MODEL = "nomic-embed-text"
    DIM = 768

    def __init__(self, base_url: str = "http://localhost:11434", model: str = ""):
        self._base_url = base_url.rstrip("/")
        self.MODEL = model or self.DEFAULT_MODEL
        self._fallback = SimpleEmbeddingProvider()
        self._failed = False

    @property
    def model_name(self) -> str:
        return f"ollama/{self.MODEL}" if not self._failed else self._fallback.model_name

    @property
    def dimensions(self) -> int:
        return self.DIM if not self._failed else self._fallback.dimensions

    def _embed_single(self, text: str) -> list[float] | None:
        if self._failed:
            return None
        try:
            resp = requests.post(
                f"{self._base_url}/api/embeddings",
                json={"model": self.MODEL, "prompt": text},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("embedding", [])
        except Exception:
            self._failed = True
            return None

    def embed_query(self, text: str) -> list[float]:
        result = self._embed_single(text)
        return result if result else self._fallback.embed_query(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results = []
        for t in texts:
            emb = self._embed_single(t)
            if emb is None:
                return self._fallback.embed_batch(texts)
            results.append(emb)
        return results


class FallbackEmbeddingChain(EmbeddingProvider):
    """Tries multiple embedding providers in order. First success wins."""

    def __init__(self, providers: list[EmbeddingProvider]):
        self._providers = providers
        self._active: EmbeddingProvider | None = None

    @property
    def model_name(self) -> str:
        if self._active:
            return self._active.model_name
        return self._providers[0].model_name if self._providers else "none"

    @property
    def dimensions(self) -> int:
        if self._active:
            return self._active.dimensions
        return self._providers[0].dimensions if self._providers else 128

    def _find_active(self) -> EmbeddingProvider:
        if self._active:
            return self._active
        for p in self._providers:
            try:
                test = p.embed_query("test")
                if test and len(test) > 0:
                    self._active = p
                    return p
            except Exception:
                continue
        fallback = SimpleEmbeddingProvider()
        self._active = fallback
        return fallback

    def embed_query(self, text: str) -> list[float]:
        return self._find_active().embed_query(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return self._find_active().embed_batch(texts)


# ---------------------------------------------------------------------------
# FTS Search
# ---------------------------------------------------------------------------

class FTSSearch:
    """Full-text search via SQLite FTS5 with BM25 scoring and query expansion."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @staticmethod
    def tokenize_query(query: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z0-9]+", query.lower())
        return [t for t in tokens if len(t) > 1]

    @staticmethod
    def _expand_query(query: str) -> str:
        """Use query expansion for richer FTS matching, with fallback."""
        try:
            from ghost_query_expansion import expand_query_for_fts
            result = expand_query_for_fts(query)
            if result["expanded"]:
                return result["expanded"]
        except ImportError:
            pass
        tokens = re.findall(r"[a-zA-Z0-9]+", query.lower())
        tokens = [t for t in tokens if len(t) > 1]
        return " OR ".join(f'"{t}"' for t in tokens) if tokens else ""

    def search(self, query: str, limit: int = 20) -> list[dict]:
        expanded = self._expand_query(query)
        if not expanded:
            return []

        tokens = self.tokenize_query(query)
        if not tokens:
            return []

        # Try AND matching first for precision, fall back to expanded OR
        match_expr = " AND ".join(f'"{t}"' for t in tokens)
        try:
            rows = self._conn.execute(
                """
                SELECT id, path, source, text,
                       bm25(chunks_fts) AS rank
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match_expr, limit),
            ).fetchall()
            if not rows:
                raise sqlite3.OperationalError("no AND results, try expanded")
        except sqlite3.OperationalError:
            try:
                rows = self._conn.execute(
                    """
                    SELECT id, path, source, text,
                           bm25(chunks_fts) AS rank
                    FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (expanded, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []

        results = []
        for row in rows:
            raw_rank = abs(row[4])
            score = 1.0 / (1.0 + raw_rank)
            results.append({
                "id": row[0],
                "path": row[1],
                "source": row[2],
                "text": row[3],
                "score": score,
            })
        return results


# ---------------------------------------------------------------------------
# Vector Search
# ---------------------------------------------------------------------------

class VectorSearch:
    """Cosine-similarity search over stored embeddings."""

    def __init__(self, conn: sqlite3.Connection, provider: EmbeddingProvider):
        self._conn = conn
        self._provider = provider

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        query_vec = self._provider.embed_query(query)
        rows = self._conn.execute(
            "SELECT id, path, source, text, embedding, updated_at FROM chunks WHERE embedding IS NOT NULL"
        ).fetchall()

        scored: list[dict] = []
        for row in rows:
            try:
                stored_vec = json.loads(row[4])
            except (json.JSONDecodeError, TypeError):
                continue
            if len(stored_vec) != len(query_vec):
                continue
            sim = self._cosine(query_vec, stored_vec)
            scored.append({
                "id": row[0],
                "path": row[1],
                "source": row[2],
                "text": row[3],
                "score": max(0.0, sim),
                "updated_at": row[5],
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]


# ---------------------------------------------------------------------------
# Hybrid Merge
# ---------------------------------------------------------------------------

class HybridMerge:
    """Combines FTS + vector results with weighted linear scoring."""

    def __init__(self, vector_weight: float = 0.7, text_weight: float = 0.3):
        self.vector_weight = vector_weight
        self.text_weight = text_weight

    def merge(self, fts_results: list[dict], vec_results: list[dict]) -> list[dict]:
        by_id: dict[str, dict] = {}

        for r in vec_results:
            by_id[r["id"]] = {
                **r,
                "vec_score": r["score"],
                "fts_score": 0.0,
            }

        for r in fts_results:
            if r["id"] in by_id:
                by_id[r["id"]]["fts_score"] = r["score"]
            else:
                by_id[r["id"]] = {
                    **r,
                    "vec_score": 0.0,
                    "fts_score": r["score"],
                }

        for entry in by_id.values():
            entry["score"] = (
                self.vector_weight * entry["vec_score"]
                + self.text_weight * entry["fts_score"]
            )

        merged = sorted(by_id.values(), key=lambda x: x["score"], reverse=True)
        return merged


# ---------------------------------------------------------------------------
# Temporal Decay
# ---------------------------------------------------------------------------

class TemporalDecay:
    """Exponential decay: score *= exp(-ln2/half_life * age_days)."""

    def __init__(self, half_life_days: float = 30.0):
        self.half_life_days = half_life_days

    def apply(self, results: list[dict], now: float = None) -> list[dict]:
        now = now or time.time()
        for r in results:
            updated = r.get("updated_at")
            path = r.get("path", "")
            if Path(path).name in EVERGREEN_NAMES:
                continue
            if updated:
                age_days = (now - updated) / 86400.0
                if age_days > 0:
                    decay = math.exp(-math.log(2) / self.half_life_days * age_days)
                    r["score"] *= decay
        return results


# ---------------------------------------------------------------------------
# MMR Reranking
# ---------------------------------------------------------------------------

class MMR:
    """Maximal Marginal Relevance for diversity in results."""

    def __init__(self, lambda_param: float = 0.7):
        self.lambda_param = lambda_param

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        set_a = set(a.lower().split())
        set_b = set(b.lower().split())
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersection / union if union else 0.0

    def rerank(self, results: list[dict], max_results: int = 10) -> list[dict]:
        if len(results) <= 1:
            return results[:max_results]

        max_score = max(r["score"] for r in results) if results else 1.0
        if max_score == 0:
            max_score = 1.0

        remaining = list(results)
        selected: list[dict] = []

        while remaining and len(selected) < max_results:
            best_idx = -1
            best_mmr = -float("inf")

            for i, candidate in enumerate(remaining):
                relevance = candidate["score"] / max_score

                max_sim = 0.0
                for s in selected:
                    sim = self._jaccard(candidate.get("text", ""), s.get("text", ""))
                    if sim > max_sim:
                        max_sim = sim

                mmr_score = self.lambda_param * relevance - (1 - self.lambda_param) * max_sim
                if mmr_score > best_mmr:
                    best_mmr = mmr_score
                    best_idx = i

            if best_idx >= 0:
                selected.append(remaining.pop(best_idx))
            else:
                break

        return selected


# ---------------------------------------------------------------------------
# Hybrid Memory Manager
# ---------------------------------------------------------------------------

class HybridMemoryManager:
    """Main orchestrator: index, sync, search, save."""

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY,
        source TEXT DEFAULT 'memory',
        hash TEXT NOT NULL,
        mtime INTEGER,
        size INTEGER
    );
    CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        source TEXT DEFAULT 'memory',
        start_line INTEGER,
        end_line INTEGER,
        hash TEXT NOT NULL,
        text TEXT NOT NULL,
        embedding TEXT,
        updated_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
    """

    FTS_SQL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        text,
        id UNINDEXED,
        path UNINDEXED,
        source UNINDEXED,
        content=chunks,
        content_rowid=rowid
    );
    """

    FTS_TRIGGERS = """
    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, text, id, path, source)
        VALUES (new.rowid, new.text, new.id, new.path, new.source);
    END;
    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, text, id, path, source)
        VALUES ('delete', old.rowid, old.text, old.id, old.path, old.source);
    END;
    CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, text, id, path, source)
        VALUES ('delete', old.rowid, old.text, old.id, old.path, old.source);
        INSERT INTO chunks_fts(rowid, text, id, path, source)
        VALUES (new.rowid, new.text, new.id, new.path, new.source);
    END;
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self.db_path = str(db_path or DEFAULT_DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._provider = embedding_provider or SimpleEmbeddingProvider()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

        self._fts = FTSSearch(self._conn)
        self._vec = VectorSearch(self._conn, self._provider)
        self._merge = HybridMerge()
        self._decay = TemporalDecay()
        self._mmr = MMR()
        self._migrate_old_db()

    def _init_schema(self):
        self._conn.executescript(self.SCHEMA_SQL)
        try:
            self._conn.executescript(self.FTS_SQL)
            self._conn.executescript(self.FTS_TRIGGERS)
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        self._conn.commit()

    def _migrate_old_db(self):
        """Import entries from the old ghost_vector_memory.py database if present."""
        old_db = GHOST_DIR / "vector_memory.db"
        if not old_db.exists():
            return
        already = self._conn.execute("SELECT value FROM meta WHERE key='migrated_old_vector'").fetchone()
        if already:
            return
        try:
            old_conn = sqlite3.connect(str(old_db))
            rows = old_conn.execute("SELECT id, content, summary, embedding, metadata, created_at, memory_type, tags, source FROM memories").fetchall()
            old_conn.close()
            now_ts = int(time.time())
            for row in rows:
                mem_id, content, summary, emb_json, meta_json, created_at, mtype, tags_json, source = row
                path = f"memory://{mtype}/{mem_id}"
                file_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                self._conn.execute(
                    "INSERT OR IGNORE INTO files (path, source, hash, mtime, size) VALUES (?,?,?,?,?)",
                    (path, source or "migrated", file_hash, now_ts, len(content)),
                )
                chunk_id = f"mig_{mem_id}"
                embedding = emb_json
                self._conn.execute(
                    "INSERT OR IGNORE INTO chunks (id, path, source, start_line, end_line, hash, text, embedding, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (chunk_id, path, source or "migrated", 0, 0, file_hash, content, embedding, now_ts),
                )
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("migrated_old_vector", "1"))
            self._conn.commit()
        except Exception:
            pass

    def index_file(self, path: str, source: str = "memory") -> int:
        """Read a file, chunk it, embed chunks, store everything. Returns chunk count."""
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return 0

        stat = Path(path).stat()
        file_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

        existing = self._conn.execute("SELECT hash FROM files WHERE path=?", (path,)).fetchone()
        if existing and existing[0] == file_hash:
            return 0

        self._conn.execute("DELETE FROM chunks WHERE path=?", (path,))
        self._conn.execute(
            "INSERT OR REPLACE INTO files (path, source, hash, mtime, size) VALUES (?,?,?,?,?)",
            (path, source, file_hash, int(stat.st_mtime), stat.st_size),
        )

        chunks = chunk_markdown(text)
        if not chunks:
            self._conn.commit()
            return 0

        texts_to_embed = [c["text"] for c in chunks]
        embeddings = self._provider.embed_batch(texts_to_embed)

        now_ts = int(time.time())
        for i, chunk in enumerate(chunks):
            start = chunk["start_line"]
            chunk_id = f"c_{hashlib.sha256(f'{path}:{start}'.encode()).hexdigest()[:12]}"
            emb_json = json.dumps(embeddings[i]) if i < len(embeddings) else None
            self._conn.execute(
                "INSERT OR REPLACE INTO chunks (id, path, source, start_line, end_line, hash, text, embedding, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (chunk_id, path, source, chunk["start_line"], chunk["end_line"], chunk["hash"], chunk["text"], emb_json, now_ts),
            )

        self._conn.commit()
        return len(chunks)

    def sync(self, memory_dir: str | Path | None = None) -> dict:
        """Scan a directory, index new/changed files, remove stale entries."""
        memory_dir = Path(memory_dir or (GHOST_DIR / "memory"))
        if not memory_dir.exists():
            return {"indexed": 0, "removed": 0}

        indexed = 0
        current_paths = set()

        for fpath in memory_dir.rglob("*"):
            if fpath.is_dir() or fpath.suffix in {".pyc", ".db", ".db-journal", ".db-wal"}:
                continue
            path_str = str(fpath)
            current_paths.add(path_str)
            count = self.index_file(path_str, source="sync")
            if count > 0:
                indexed += count

        known_paths = {
            row[0] for row in
            self._conn.execute("SELECT path FROM files WHERE source IN ('memory', 'sync')").fetchall()
        }
        stale = known_paths - current_paths
        for p in stale:
            if p.startswith("memory://"):
                continue
            self._conn.execute("DELETE FROM chunks WHERE path=?", (p,))
            self._conn.execute("DELETE FROM files WHERE path=?", (p,))
        if stale:
            self._conn.commit()

        return {"indexed": indexed, "removed": len(stale)}

    def search(self, query: str, max_results: int = 10, min_score: float = 0.05) -> list[dict]:
        """Run hybrid search: FTS + vector -> merge -> decay -> MMR."""
        fts_results = self._fts.search(query, limit=max_results * 3)
        vec_results = self._vec.search(query, limit=max_results * 3)
        merged = self._merge.merge(fts_results, vec_results)
        merged = self._decay.apply(merged)
        merged = [r for r in merged if r["score"] >= min_score]
        merged.sort(key=lambda x: x["score"], reverse=True)
        final = self._mmr.rerank(merged, max_results=max_results)
        return final

    def save(self, content: str, metadata: dict | None = None,
             memory_type: str = "note", source: str = "user",
             tags: list[str] | None = None) -> dict:
        """Save a memory entry directly (not from file)."""
        mem_id = hashlib.sha256(f"{content}{time.time()}".encode()).hexdigest()[:16]
        path = f"memory://{memory_type}/{mem_id}"
        file_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        now_ts = int(time.time())

        meta = metadata or {}
        meta["memory_type"] = memory_type
        meta["tags"] = tags or []
        meta["source"] = source

        self._conn.execute(
            "INSERT OR REPLACE INTO files (path, source, hash, mtime, size) VALUES (?,?,?,?,?)",
            (path, source, file_hash, now_ts, len(content)),
        )

        chunks = chunk_markdown(content)
        if not chunks:
            chunks = [{"start_line": 0, "end_line": 0, "text": content, "hash": file_hash}]

        embeddings = self._provider.embed_batch([c["text"] for c in chunks])

        for i, chunk in enumerate(chunks):
            chunk_id = f"m_{mem_id}_{i}"
            emb_json = json.dumps(embeddings[i]) if i < len(embeddings) else None
            self._conn.execute(
                "INSERT OR REPLACE INTO chunks (id, path, source, start_line, end_line, hash, text, embedding, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (chunk_id, path, source, chunk["start_line"], chunk["end_line"], chunk["hash"], chunk["text"], emb_json, now_ts),
            )

        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"meta:{path}", json.dumps(meta)),
        )
        self._conn.commit()

        return {"memory_id": mem_id, "path": path, "chunks": len(chunks)}

    def delete(self, memory_id: str) -> bool:
        """Delete a memory by its ID or path prefix."""
        like_pattern = f"%{memory_id}%"
        rows = self._conn.execute("SELECT path FROM files WHERE path LIKE ?", (like_pattern,)).fetchall()
        if not rows:
            return False
        for (path,) in rows:
            self._conn.execute("DELETE FROM chunks WHERE path=?", (path,))
            self._conn.execute("DELETE FROM files WHERE path=?", (path,))
            self._conn.execute("DELETE FROM meta WHERE key=?", (f"meta:{path}",))
        self._conn.commit()
        return True

    def index_session_transcripts(self, sessions_dir: str | Path | None = None) -> int:
        """Index session transcript files for semantic search.

        Scans ~/.ghost/memory/sessions/ for markdown session files and indexes them.
        Returns count of newly indexed chunks.
        """
        sessions_dir = Path(sessions_dir or (GHOST_DIR / "memory" / "sessions"))
        if not sessions_dir.exists():
            return 0

        indexed = 0
        for fpath in sessions_dir.glob("*.md"):
            count = self.index_file(str(fpath), source="session")
            indexed += count
        return indexed

    def stats(self) -> dict:
        total_chunks = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        total_files = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        sources = {}
        for row in self._conn.execute("SELECT source, COUNT(*) FROM files GROUP BY source"):
            sources[row[0]] = row[1]
        session_count = self._conn.execute(
            "SELECT COUNT(*) FROM files WHERE source='session'"
        ).fetchone()[0]
        return {
            "total_chunks": total_chunks,
            "total_files": total_files,
            "session_files": session_count,
            "by_source": sources,
            "db_path": self.db_path,
            "embedding_model": self._provider.model_name,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_manager: Optional[HybridMemoryManager] = None


def get_manager(api_key: str | None = None, auth_store=None,
                cfg: dict = None) -> HybridMemoryManager:
    global _manager
    if _manager is None:
        provider = _build_embedding_provider(api_key, auth_store, cfg)
        _manager = HybridMemoryManager(embedding_provider=provider)
    return _manager


def _build_embedding_provider(api_key: str | None = None,
                              auth_store=None,
                              cfg: dict = None) -> EmbeddingProvider:
    """Build a multi-provider embedding chain with automatic fallback.

    Priority: OpenRouter → Gemini → Ollama → Simple (hash-based)
    Model IDs are resolved from config.tool_models with built-in defaults.
    """
    try:
        from ghost_config_tool import get_tool_model
        or_model = get_tool_model("embedding_openrouter", cfg)
        gemini_model = get_tool_model("embedding_gemini", cfg)
        ollama_model = get_tool_model("embedding_ollama", cfg)
    except ImportError:
        or_model = ""
        gemini_model = ""
        ollama_model = ""

    candidates = {}
    if api_key:
        candidates["openrouter"] = OpenRouterEmbeddingProvider(api_key, model=or_model)
    gemini_key = os.environ.get("GOOGLE_AI_API_KEY", "")
    if not gemini_key and auth_store:
        try:
            gemini_key = auth_store.get_api_key("google") or ""
        except Exception:
            pass
    if gemini_key:
        candidates["gemini"] = GeminiEmbeddingProvider(gemini_key, model=gemini_model)
    candidates["ollama"] = OllamaEmbeddingProvider(model=ollama_model)

    # Keyless local neural embeddings — a strong default that needs no API key
    # and no external service. Enabled unless explicitly turned off.
    local_neural = None
    if (cfg or {}).get("enable_neural_embeddings", True):
        try:
            local_neural = LocalNeuralEmbeddingProvider()
            candidates["local"] = local_neural
        except Exception:
            local_neural = None

    chain = (cfg or {}).get("provider_chains", {}).get(
        "embeddings", ["openrouter", "gemini", "local", "ollama"])
    providers: list[EmbeddingProvider] = []
    seen = set()
    for pid in chain:
        if pid in candidates and pid not in seen:
            seen.add(pid)
            providers.append(candidates[pid])
    for pid, prov in candidates.items():
        if pid not in seen:
            providers.append(prov)

    if not providers:
        return local_neural or SimpleEmbeddingProvider()

    if len(providers) == 1:
        return providers[0]

    return FallbackEmbeddingChain(providers)


# ---------------------------------------------------------------------------
# Tool Builders
# ---------------------------------------------------------------------------

def build_hybrid_memory_tools(api_key: str | None = None,
                              cfg: dict = None) -> list[dict]:
    """Build tool defs for Ghost's tool registry. Replaces ghost_vector_memory tools."""
    get_manager(api_key, cfg=cfg)
    return [
        _make_semantic_memory_save(api_key),
        _make_semantic_memory_search(api_key),
        _make_hybrid_memory_search(api_key),
        _make_index_sessions(api_key),
    ]


def _make_semantic_memory_save(api_key: str | None = None) -> dict:
    def execute(content: str, summary: str = "", memory_type: str = "note",
                tags: list = None, source: str = "user", metadata: dict = None):
        mgr = get_manager(api_key)
        result = mgr.save(content, metadata=metadata, memory_type=memory_type,
                          source=source, tags=tags)
        return json.dumps({
            "memory_id": result["memory_id"],
            "saved": True,
            "type": memory_type,
            "chunks": result["chunks"],
            "message": f"Memory saved with ID: {result['memory_id']}",
        })

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
                "metadata": {"type": "object", "description": "Additional structured data"},
            },
            "required": ["content"],
        },
        "execute": execute,
    }


def _make_semantic_memory_search(api_key: str | None = None) -> dict:
    def execute(query: str, top_k: int = 5, memory_type: str = None, tags: list = None):
        mgr = get_manager(api_key)
        results = mgr.search(query, max_results=top_k)

        if memory_type:
            results = [r for r in results if memory_type in r.get("path", "")]
        if tags:
            results = [r for r in results
                       if any(t.lower() in r.get("text", "").lower() for t in tags)]

        formatted = []
        for r in results:
            formatted.append({
                "id": r["id"],
                "content": r["text"][:500] + "..." if len(r.get("text", "")) > 500 else r.get("text", ""),
                "path": r.get("path", ""),
                "score": round(r["score"], 3),
                "source": r.get("source", ""),
            })

        return json.dumps({
            "query": query,
            "results": formatted,
            "count": len(formatted),
        })

    return {
        "name": "semantic_memory_search",
        "description": "Search memories by meaning/semantics. Finds related memories even when phrased differently.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "top_k": {"type": "integer", "default": 5, "description": "Number of results"},
                "memory_type": {"type": "string", "description": "Filter by memory type"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags"},
            },
            "required": ["query"],
        },
        "execute": execute,
    }


def _make_index_sessions(api_key: str | None = None) -> dict:
    def execute():
        mgr = get_manager(api_key)
        count = mgr.index_session_transcripts()
        stats = mgr.stats()
        return json.dumps({
            "indexed_chunks": count,
            "total_session_files": stats.get("session_files", 0),
            "total_chunks": stats["total_chunks"],
            "embedding_model": stats["embedding_model"],
        })

    return {
        "name": "memory_index_sessions",
        "description": (
            "Index all session transcript files from ~/.ghost/memory/sessions/ "
            "into the hybrid memory system for semantic search. Run this to make "
            "past conversation sessions searchable."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
        "execute": execute,
    }


def _make_hybrid_memory_search(api_key: str | None = None) -> dict:
    def execute(query: str, top_k: int = 5):
        mgr = get_manager(api_key)
        results = mgr.search(query, max_results=top_k)

        formatted = []
        for r in results:
            match_type = "hybrid"
            if r.get("vec_score", 0) > 0 and r.get("fts_score", 0) > 0:
                match_type = "hybrid"
            elif r.get("vec_score", 0) > 0:
                match_type = "semantic"
            else:
                match_type = "text"

            formatted.append({
                "id": r["id"],
                "content": r["text"][:500] + "..." if len(r.get("text", "")) > 500 else r.get("text", ""),
                "path": r.get("path", ""),
                "score": round(r["score"], 3),
                "match_type": match_type,
                "source": r.get("source", ""),
            })

        return json.dumps({
            "query": query,
            "results": formatted,
            "count": len(formatted),
        })

    return {
        "name": "hybrid_memory_search",
        "description": "Search memories using both semantic similarity and text matching. Best comprehensive search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "top_k": {"type": "integer", "default": 5, "description": "Number of results"},
            },
            "required": ["query"],
        },
        "execute": execute,
    }
