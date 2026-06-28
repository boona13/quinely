"""Memory API — uses live daemon MemoryDB when embedded, else opens its own."""

from flask import Blueprint, jsonify, request

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ghost_memory import MemoryDB

bp = Blueprint("memory", __name__)

_standalone_db = None


def _get_db():
    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    if daemon and daemon.memory_db:
        return daemon.memory_db

    global _standalone_db
    if _standalone_db is None:
        _standalone_db = MemoryDB()
    return _standalone_db


@bp.route("/api/memory/stats")
def memory_stats():
    db = _get_db()
    stats = db.stats()
    return jsonify(stats)


@bp.route("/api/memory/search")
def memory_search():
    db = _get_db()
    q = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 50))
    if not q:
        return jsonify({"results": [], "query": ""})
    results = db.search(q, limit=limit)
    return jsonify({"results": results, "query": q})


@bp.route("/api/memory/recent")
def memory_recent():
    db = _get_db()
    limit = int(request.args.get("limit", 20))
    results = db.recent(limit=limit)
    return jsonify({"results": results})


@bp.route("/api/memory/<int:memory_id>", methods=["DELETE"])
def memory_delete(memory_id):
    db = _get_db()
    try:
        db.delete(memory_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/memory/prune", methods=["POST"])
def memory_prune():
    data = request.get_json(silent=True) or {}
    keep = data.get("keep", 1000)
    db = _get_db()
    db.prune(keep)
    stats = db.stats()
    return jsonify({"ok": True, "stats": stats})


import re as _re


def _split_tags(raw):
    """Parse a stored tag string into a clean, de-duped, lowercased list."""
    if not raw:
        return []
    parts = _re.split(r"[,\s]+", str(raw).strip())
    seen, out = set(), []
    for p in parts:
        t = p.strip().lower().lstrip("#")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= 8:
            break
    return out


def _short_label(row):
    """A compact one-line label for a memory node."""
    txt = (row.get("source_preview") or row.get("content") or "").strip()
    txt = _re.sub(r"\s+", " ", txt)
    if len(txt) > 52:
        txt = txt[:52].rstrip() + "…"
    return txt or f"#{row.get('id')}"


@bp.route("/api/memory/graph")
def memory_graph():
    """Build a knowledge-graph view of memory from the existing FTS store.

    Read-only and fully defensive: memories cluster around per-type hub nodes,
    and memories that share tags are cross-linked. No schema changes, no new
    store — this is purely a visualization over data Ghost already keeps.
    """
    try:
        db = _get_db()
        limit = max(10, min(int(request.args.get("limit", 180)), 400))
        rows = db.recent(limit=limit) or []

        try:
            stats = db.stats() or {}
        except Exception:
            stats = {}
        global_by_type = stats.get("by_type", {}) if isinstance(stats, dict) else {}

        nodes = []
        links = []
        type_hubs = {}          # type -> hub node id
        tag_members = {}        # tag -> [memory node id]

        for row in rows:
            mid = row.get("id")
            if mid is None:
                continue
            mtype = (row.get("type") or "note").strip() or "note"
            node_id = f"m:{mid}"
            tags = _split_tags(row.get("tags"))

            nodes.append({
                "id": node_id,
                "kind": "memory",
                "type": mtype,
                "label": _short_label(row),
                "preview": _re.sub(r"\s+", " ", (row.get("content") or "")).strip()[:400],
                "tags": tags,
                "ts": row.get("timestamp") or "",
                "tokens": row.get("tokens_used") or 0,
                "skill": (row.get("skill") or "").strip(),
            })

            # Type hub (created lazily) + membership link.
            if mtype not in type_hubs:
                hub_id = f"t:{mtype}"
                type_hubs[mtype] = hub_id
                nodes.append({
                    "id": hub_id,
                    "kind": "type",
                    "type": mtype,
                    "label": mtype,
                    "count": int(global_by_type.get(mtype, 0)) or 0,
                })
            links.append({"s": node_id, "t": type_hubs[mtype], "kind": "type"})

            for tag in tags:
                tag_members.setdefault(tag, []).append(node_id)

        # Tag cross-links: connect memories sharing a tag. Skip ultra-common
        # tags (they'd create a hairball) and accumulate a weight per pair.
        pair_weight = {}
        for tag, members in tag_members.items():
            if len(members) < 2 or len(members) > 24:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    key = (a, b) if a < b else (b, a)
                    pair_weight[key] = pair_weight.get(key, 0) + 1

        # Keep only each node's strongest few tag-neighbors to stay readable.
        by_node = {}
        for (a, b), w in pair_weight.items():
            by_node.setdefault(a, []).append((w, b))
            by_node.setdefault(b, []).append((w, a))
        kept = set()
        for n, lst in by_node.items():
            lst.sort(reverse=True)
            for w, other in lst[:4]:
                key = (n, other) if n < other else (other, n)
                if key in kept:
                    continue
                kept.add(key)
                links.append({"s": key[0], "t": key[1], "kind": "tag",
                              "w": pair_weight.get(key, 1)})

        types_present = {ty: int(global_by_type.get(ty, 0)) or sum(
            1 for nd in nodes if nd.get("kind") == "memory" and nd.get("type") == ty
        ) for ty in type_hubs}

        return jsonify({
            "nodes": nodes,
            "links": links,
            "types": types_present,
            "total": int(stats.get("total", 0)) if isinstance(stats, dict) else 0,
            "shown": sum(1 for n in nodes if n.get("kind") == "memory"),
        })
    except Exception as e:
        return jsonify({"nodes": [], "links": [], "types": {}, "total": 0,
                        "shown": 0, "error": str(e)}), 200
