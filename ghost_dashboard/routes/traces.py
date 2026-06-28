"""Run-trace API — per-invocation traces (trigger → model/tool spans → outcome).

Backed by the in-process tracer singleton (ghost_trace), which keeps a ring
buffer of recent runs and appends each completed run to ~/.ghost/logs/runs.jsonl.
"""

import sys
from pathlib import Path
from flask import Blueprint, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from ghost_trace import get_tracer

bp = Blueprint("traces", __name__, url_prefix="/api/traces")


@bp.route("", methods=["GET"])
@bp.route("/", methods=["GET"])
def list_traces():
    """List recent runs (summaries). Filter by ?source= and ?status=."""
    limit = request.args.get("limit", 50, type=int)
    source = request.args.get("source", "", type=str) or ""
    status = request.args.get("status", "", type=str) or ""
    runs = get_tracer().list_runs(limit=limit, source=source, status=status)
    return jsonify({"runs": runs, "count": len(runs)})


@bp.route("/stats", methods=["GET"])
def trace_stats():
    """Aggregate stats across the recent-runs buffer."""
    return jsonify(get_tracer().stats())


@bp.route("/<run_id>", methods=["GET"])
def get_trace(run_id):
    """Full run detail including model and tool spans."""
    run = get_tracer().get_run(run_id)
    if run is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(run)
