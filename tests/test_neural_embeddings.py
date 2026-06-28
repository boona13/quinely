"""Tests for neural embeddings + ANN vector memory (ghost_embeddings,
ghost_vector_memory, hybrid LocalNeuralEmbeddingProvider).

Run: python -m pytest tests/test_neural_embeddings.py -q
Falls back gracefully when the neural model can't be downloaded (offline):
the neural-specific assertions are skipped, hash-path assertions still run.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ghost_embeddings as E
from ghost_vector_memory import VectorMemoryStore, MemoryEntry
from datetime import datetime


def _fresh_store():
    return VectorMemoryStore(db_path=tempfile.mktemp(suffix=".db"))


def test_embedder_singleton_and_hash_fallback():
    E.configure(enable_neural=False)
    emb = E.get_embedder()
    assert emb.is_neural is False
    assert emb.dim == 128
    assert emb.model_id == "hash-128"
    v = emb.embed("hello world")
    assert len(v) == 128
    # restore default for later tests
    E.configure(enable_neural=True)


def test_hash_search_exact_token_match():
    E.configure(enable_neural=False)
    s = _fresh_store()
    s.add_text("The capital of France is Paris.", memory_type="fact")
    s.add_text("Python is a programming language.", memory_type="fact")
    res = s.search("programming language", top_k=2)
    assert res, "expected a hash-space match"
    assert "Python" in res[0][0].content
    E.configure(enable_neural=True)


def test_cache_invalidation_on_save_and_delete():
    E.configure(enable_neural=False)
    s = _fresh_store()
    mid = s.add_text("alpha beta gamma delta")
    assert s.search("alpha beta", top_k=1)
    assert s.delete(mid) is True
    # After delete the cache must refresh and return nothing for that content
    res = s.search("alpha beta", top_k=1)
    assert all("alpha beta gamma" not in e.content for e, _ in res)
    E.configure(enable_neural=True)


def test_mixed_legacy_and_neural_spaces():
    """A store can contain legacy hash-128 rows and neural rows at once."""
    E.configure(enable_neural=True)
    emb = E.get_embedder()
    if not emb.warmup():
        import pytest
        pytest.skip("neural model unavailable (offline)")
    s = _fresh_store()
    # Insert a legacy hash row directly (model=hash-128)
    legacy = MemoryEntry(
        id="legacy1", content="legacy hash row about kittens",
        summary="", embedding=emb.hash_embed("legacy hash row about kittens"),
        metadata={}, created_at=datetime.now().isoformat(),
        memory_type="note", tags=[], source="test")
    s.save(legacy, model="hash-128")
    # Insert a neural row
    s.add_text("My favorite language for systems work is Rust.")
    # Query that semantically matches the neural row (no shared words)
    res = s.search("which coding language do I prefer", top_k=3)
    assert res, "expected results across mixed spaces"
    assert any("Rust" in e.content for e, _ in res)


def test_neural_semantic_no_shared_words():
    E.configure(enable_neural=True)
    emb = E.get_embedder()
    if not emb.warmup():
        import pytest
        pytest.skip("neural model unavailable (offline)")
    s = _fresh_store()
    s.add_text("The user enjoys hiking in the mountains on weekends.")
    s.add_text("Quarterly revenue increased due to strong sales.")
    res = s.search("outdoor recreation activities", top_k=1)
    assert res
    assert "hiking" in res[0][0].content


def test_sync_idempotent():
    E.configure(enable_neural=True)
    s = _fresh_store()

    class _FakeMem:
        def get_recent(self, limit=20000):
            return [
                {"id": 1, "content": "first memory about cooking", "timestamp": "", "type": "note", "tags": ""},
                {"id": 2, "content": "second memory about cars", "timestamp": "", "type": "note", "tags": "auto"},
                {"id": 3, "content": "", "timestamp": "", "type": "note", "tags": ""},  # empty -> skipped
            ]

    fake = _FakeMem()
    stats1 = s.sync_from_memory(memory_db=fake)
    assert stats1["indexed"] == 2
    stats2 = s.sync_from_memory(memory_db=fake)
    assert stats2["indexed"] == 0  # already present -> idempotent


def test_hybrid_local_neural_provider():
    from ghost_hybrid_memory import LocalNeuralEmbeddingProvider
    p = LocalNeuralEmbeddingProvider()
    v = p.embed_query("hello there general")
    assert isinstance(v, list) and len(v) == p.dimensions
    batch = p.embed_batch(["a", "b"])
    assert len(batch) == 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
