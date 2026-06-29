"""
GhostNodes Pipeline Engine — chain multiple AI nodes into multi-step workflows.

The LLM describes intent, the pipeline engine builds and executes a typed
chain of node operations with intermediate result passing, caching, and
progress tracking.
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("quinely.pipeline")

GHOST_HOME = Path.home() / ".ghost"
PIPELINES_DIR = GHOST_HOME / "pipelines"
PIPELINES_DIR.mkdir(parents=True, exist_ok=True)


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class PipelineStep:
    id: str
    tool_name: str
    params: dict = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: str = ""
    started_at: float = 0
    completed_at: float = 0
    input_from: str = ""
    input_key: str = "path"
    output_key: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "params": self.params,
            "status": self.status.value,
            "result": self.result if isinstance(self.result, (str, dict, list)) else str(self.result)[:500],
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_secs": round(self.completed_at - self.started_at, 2) if self.completed_at else 0,
            "input_from": self.input_from,
            "input_key": self.input_key,
            "output_key": self.output_key,
        }


@dataclass
class Pipeline:
    id: str
    name: str
    description: str = ""
    steps: list[PipelineStep] = field(default_factory=list)
    status: PipelineStatus = PipelineStatus.DRAFT
    created_at: float = field(default_factory=time.time)
    started_at: float = 0
    completed_at: float = 0
    final_output: Any = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_secs": round(self.completed_at - self.started_at, 2) if self.completed_at else 0,
            "final_output": self.final_output,
        }


class PipelineEngine:
    """Execute multi-step AI pipelines by chaining Ghost tool calls."""

    def __init__(self, tool_registry, node_manager=None):
        self.tool_registry = tool_registry
        self.node_manager = node_manager
        self._pipelines: dict[str, Pipeline] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        self._exec_locks: dict[str, threading.Lock] = {}
        self._load_saved()

    def _validate_steps(self, steps: list[PipelineStep]) -> str | None:
        """Validate step references. Returns error string or None if valid."""
        step_ids = {s.id for s in steps}
        seen_ids = set()
        for i, step in enumerate(steps):
            if step.id in seen_ids:
                return f"Duplicate step id: {step.id}"
            seen_ids.add(step.id)

            if step.input_from:
                if step.input_from not in step_ids:
                    return f"Step '{step.id}' references unknown input_from: '{step.input_from}'"
                if step.input_from not in seen_ids:
                    return f"Step '{step.id}' has forward reference to '{step.input_from}' (must reference earlier step)"
        return None

    def create(self, name: str, steps_config: list[dict],
               description: str = "") -> Pipeline:
        """Create a pipeline from a list of step configurations."""
        pipeline_id = uuid.uuid4().hex[:10]
        steps = []
        for i, sc in enumerate(steps_config):
            step = PipelineStep(
                id=sc.get("id", f"step_{i}"),
                tool_name=sc["tool_name"],
                params=sc.get("params", {}),
                input_from=sc.get("input_from", ""),
                input_key=sc.get("input_key", "path"),
                output_key=sc.get("output_key", ""),
            )
            steps.append(step)

        validation_err = self._validate_steps(steps)
        if validation_err:
            raise ValueError(validation_err)

        pipeline = Pipeline(
            id=pipeline_id,
            name=name,
            description=description,
            steps=steps,
        )
        with self._lock:
            self._pipelines[pipeline_id] = pipeline
        self._save(pipeline)
        return pipeline

    def _get_exec_lock(self, pipeline_id: str) -> threading.Lock:
        with self._lock:
            if pipeline_id not in self._exec_locks:
                self._exec_locks[pipeline_id] = threading.Lock()
            return self._exec_locks[pipeline_id]

    def execute(self, pipeline_id: str) -> Pipeline:
        """Execute a pipeline sequentially, passing outputs between steps.

        Checks for cancellation between steps so cancel() works on running pipelines.
        Uses a per-pipeline lock to prevent concurrent execution of the same pipeline.
        """
        exec_lock = self._get_exec_lock(pipeline_id)
        if not exec_lock.acquire(blocking=False):
            raise ValueError(f"Pipeline {pipeline_id} is already running")

        try:
            return self._execute_locked(pipeline_id)
        finally:
            exec_lock.release()

    def _execute_locked(self, pipeline_id: str) -> Pipeline:
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline not found: {pipeline_id}")

        self._cancelled.discard(pipeline_id)
        pipeline.status = PipelineStatus.RUNNING
        pipeline.started_at = time.time()

        step_outputs: dict[str, Any] = {}

        for step in pipeline.steps:
            if pipeline_id in self._cancelled:
                step.status = StepStatus.SKIPPED
                continue

            step.status = StepStatus.RUNNING
            step.started_at = time.time()

            params = dict(step.params)
            if step.input_from and step.input_from in step_outputs:
                prev_output = step_outputs[step.input_from]
                input_val = self._extract_output(prev_output, step.output_key)
                params[step.input_key] = input_val

            try:
                result_str = self.tool_registry.execute(step.tool_name, params)
                try:
                    result = json.loads(result_str) if isinstance(result_str, str) else result_str
                except json.JSONDecodeError:
                    result = result_str

                if isinstance(result, dict) and result.get("status") == "error":
                    step.status = StepStatus.FAILED
                    step.error = result.get("error", "Unknown error")
                    step.result = result
                    pipeline.status = PipelineStatus.FAILED
                    break

                step.status = StepStatus.COMPLETED
                step.result = result
                step_outputs[step.id] = result

            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)[:500]
                pipeline.status = PipelineStatus.FAILED
                break
            finally:
                step.completed_at = time.time()

        if pipeline_id in self._cancelled:
            pipeline.status = PipelineStatus.CANCELLED
            self._cancelled.discard(pipeline_id)
        elif pipeline.status == PipelineStatus.RUNNING:
            pipeline.status = PipelineStatus.COMPLETED
            if pipeline.steps:
                last = pipeline.steps[-1]
                pipeline.final_output = last.result

        pipeline.completed_at = time.time()
        self._save(pipeline)
        return pipeline

    _OUTPUT_KEY_PRIORITY = ["path", "text", "result", "output", "url", "file"]

    @staticmethod
    def _extract_output(prev_output: Any, output_key: str) -> str:
        """Extract a value from a step's output using the output_key.

        If output_key is empty (auto-detect), tries common keys in priority order.
        Logs a warning if the requested key is missing rather than returning empty string.
        """
        data = prev_output
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, AttributeError):
                return data

        if isinstance(data, dict):
            if output_key:
                val = data.get(output_key)
                if val is not None:
                    return str(val) if not isinstance(val, str) else val
                log.warning("Pipeline: output_key %r not found in step output (keys: %s)",
                            output_key, list(data.keys()))

            for candidate in PipelineEngine._OUTPUT_KEY_PRIORITY:
                val = data.get(candidate)
                if val is not None and val != "":
                    return str(val) if not isinstance(val, str) else val

            non_status = {k: v for k, v in data.items()
                         if k not in ("status", "error", "elapsed_secs")}
            if len(non_status) == 1:
                val = next(iter(non_status.values()))
                return str(val) if not isinstance(val, str) else val

            return json.dumps(data)

        return str(prev_output)

    def execute_async(self, pipeline_id: str):
        """Execute a pipeline in a background thread."""
        thread = threading.Thread(
            target=self.execute, args=(pipeline_id,),
            daemon=True, name=f"pipeline-{pipeline_id}",
        )
        thread.start()

    def get(self, pipeline_id: str) -> Optional[Pipeline]:
        with self._lock:
            return self._pipelines.get(pipeline_id)

    def list_pipelines(self, limit: int = 20) -> list[dict]:
        with self._lock:
            pipelines = sorted(
                self._pipelines.values(),
                key=lambda p: p.created_at, reverse=True,
            )[:limit]
        return [p.to_dict() for p in pipelines]

    def cancel(self, pipeline_id: str) -> bool:
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            return False
        self._cancelled.add(pipeline_id)
        if pipeline.status != PipelineStatus.RUNNING:
            pipeline.status = PipelineStatus.CANCELLED
            for step in pipeline.steps:
                if step.status == StepStatus.PENDING:
                    step.status = StepStatus.SKIPPED
            pipeline.completed_at = time.time()
            self._save(pipeline)
        return True

    def delete(self, pipeline_id: str) -> bool:
        with self._lock:
            pipeline = self._pipelines.pop(pipeline_id, None)
        if not pipeline:
            return False
        save_path = PIPELINES_DIR / f"{pipeline_id}.json"
        save_path.unlink(missing_ok=True)
        return True

    def _save(self, pipeline: Pipeline):
        save_path = PIPELINES_DIR / f"{pipeline.id}.json"
        tmp_path = save_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(pipeline.to_dict(), default=str, indent=2), encoding="utf-8")
        tmp_path.replace(save_path)

    def _load_saved(self):
        for f in PIPELINES_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                steps = [
                    PipelineStep(
                        id=s["id"], tool_name=s["tool_name"],
                        params=s.get("params", {}),
                        status=StepStatus(s.get("status", "pending")),
                        result=s.get("result"),
                        error=s.get("error", ""),
                        input_from=s.get("input_from", ""),
                        input_key=s.get("input_key", "path"),
                        output_key=s.get("output_key", ""),
                    )
                    for s in data.get("steps", [])
                ]
                pipeline = Pipeline(
                    id=data["id"], name=data["name"],
                    description=data.get("description", ""),
                    steps=steps,
                    status=PipelineStatus(data.get("status", "draft")),
                    created_at=data.get("created_at", 0),
                    completed_at=data.get("completed_at", 0),
                    final_output=data.get("final_output"),
                )
                self._pipelines[pipeline.id] = pipeline
            except Exception as e:
                log.warning("Failed to load pipeline %s: %s", f.name, e)


# ═════════════════════════════════════════════════════════════════════
#  TOOL BUILDER
# ═════════════════════════════════════════════════════════════════════

def build_pipeline_tools(pipeline_engine: PipelineEngine):
    """Build tools for creating and managing AI pipelines."""

    def execute_create(name="", description="", steps="", **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name is required"})
        if not steps:
            return json.dumps({"status": "error", "error": "steps is required (JSON array)"})

        try:
            steps_list = json.loads(steps) if isinstance(steps, str) else steps
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "error": f"Invalid steps JSON: {e}"})

        if not isinstance(steps_list, list) or not steps_list:
            return json.dumps({"status": "error", "error": "steps must be a non-empty array"})

        for i, s in enumerate(steps_list):
            if "tool_name" not in s:
                return json.dumps({"status": "error", "error": f"Step {i} missing tool_name"})

        try:
            pipeline = pipeline_engine.create(name, steps_list, description=description)
            return json.dumps({"status": "ok", "pipeline": pipeline.to_dict()}, default=str)
        except ValueError as e:
            return json.dumps({"status": "error", "error": str(e)})

    def execute_run(pipeline_id="", async_mode="false", **_kw):
        if not pipeline_id:
            return json.dumps({"status": "error", "error": "pipeline_id is required"})
        try:
            if str(async_mode).lower() in ("true", "1", "yes"):
                pipeline_engine.execute_async(pipeline_id)
                return json.dumps({"status": "ok", "message": f"Pipeline {pipeline_id} started in background"})
            pipeline = pipeline_engine.execute(pipeline_id)
            return json.dumps({"status": "ok", "pipeline": pipeline.to_dict()}, default=str)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

    def execute_status(pipeline_id="", **_kw):
        if not pipeline_id:
            return json.dumps({"status": "error", "error": "pipeline_id is required"})
        pipeline = pipeline_engine.get(pipeline_id)
        if not pipeline:
            return json.dumps({"status": "error", "error": "Pipeline not found"})
        return json.dumps({"status": "ok", "pipeline": pipeline.to_dict()}, default=str)

    def execute_list(limit=20, **_kw):
        pipelines = pipeline_engine.list_pipelines(limit=limit)
        return json.dumps({"status": "ok", "count": len(pipelines), "pipelines": pipelines}, default=str)

    def execute_cancel(pipeline_id="", **_kw):
        if not pipeline_id:
            return json.dumps({"status": "error", "error": "pipeline_id is required"})
        ok = pipeline_engine.cancel(pipeline_id)
        return json.dumps({"status": "ok" if ok else "error"})

    return [
        {
            "name": "pipeline_create",
            "description": (
                "Create a multi-step AI pipeline that chains GhostNode tools together.\n\n"
                "Each step specifies a tool_name and params. Steps can reference output from "
                "previous steps via input_from (step id). The pipeline engine automatically "
                "passes file paths between steps.\n\n"
                "Example steps JSON:\n"
                '[{"id":"gen","tool_name":"text_to_image_local","params":{"prompt":"a cat"}},\n'
                ' {"id":"nobg","tool_name":"remove_background","params":{},"input_from":"gen","input_key":"image_path"}]'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Pipeline name"},
                    "description": {"type": "string", "description": "What this pipeline does"},
                    "steps": {
                        "type": "string",
                        "description": (
                            "JSON array of steps. Each step: "
                            '{"id":"step_0","tool_name":"...","params":{...},"input_from":"prev_step_id",'
                            '"input_key":"param_name","output_key":"text"} '
                            '(output_key: key to extract from previous step output, e.g. "path","text","result"; auto-detected if omitted)'
                        ),
                    },
                },
                "required": ["name", "steps"],
            },
            "execute": execute_create,
        },
        {
            "name": "pipeline_run",
            "description": "Execute a created pipeline. Runs each step sequentially, passing outputs forward.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_id": {"type": "string", "description": "Pipeline ID from pipeline_create"},
                    "async_mode": {"type": "string", "description": "Set to 'true' to run in background (default: false)"},
                },
                "required": ["pipeline_id"],
            },
            "execute": execute_run,
        },
        {
            "name": "pipeline_status",
            "description": "Check the status and results of a pipeline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_id": {"type": "string", "description": "Pipeline ID"},
                },
                "required": ["pipeline_id"],
            },
            "execute": execute_status,
        },
        {
            "name": "pipeline_list",
            "description": "List all pipelines (recent first).",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
            },
            "execute": execute_list,
        },
        {
            "name": "pipeline_cancel",
            "description": "Cancel a running pipeline. Running steps complete but remaining steps are skipped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_id": {"type": "string", "description": "Pipeline ID to cancel"},
                },
                "required": ["pipeline_id"],
            },
            "execute": execute_cancel,
        },
    ]
