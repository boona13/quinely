"""
Ghost LLM Task — Schema-validated JSON-only LLM subtasks.

Allows Ghost to call the LLM for structured, tools-disabled subtasks:
  - Data extraction from text
  - Classification / categorization
  - Structured summaries
  - Schema-validated JSON output

Uses jsonschema for validation when a schema is provided.
"""

import json
import logging
import re

log = logging.getLogger("quinely.llm_task")

JSON_SYSTEM_PROMPT = (
    "You are a JSON-only task executor. You MUST respond with ONLY valid JSON. "
    "No markdown fences. No commentary. No explanation. No tools. "
    "Just a single JSON object or array as specified in the task."
)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2:
            start = 1
            end = len(lines)
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].strip().startswith("```"):
                    end = i
                    break
            text = "\n".join(lines[start:end]).strip()
    return text


def _validate_schema(data, schema: dict) -> tuple[bool, str]:
    """Validate data against JSON Schema. Returns (ok, error_message)."""
    try:
        import jsonschema
        jsonschema.validate(instance=data, schema=schema)
        return True, ""
    except ImportError:
        log.debug("jsonschema not installed, skipping validation")
        return True, ""
    except Exception as e:
        return False, str(e)


def run_llm_task(engine, prompt: str, input_json=None,
                 schema: dict = None, max_tokens: int = 2048,
                 temperature: float = 0.1) -> dict:
    """Execute a structured JSON-only LLM task.

    Args:
        engine: ToolLoopEngine instance
        prompt: Task instruction
        input_json: Optional input data (dict or list)
        schema: Optional JSON Schema for output validation
        max_tokens: Max tokens for response
        temperature: LLM temperature

    Returns:
        {"ok": True, "data": <parsed JSON>} or {"ok": False, "error": str}
    """
    user_message = f"TASK: {prompt}"
    if input_json is not None:
        input_str = json.dumps(input_json, indent=2, default=str)
        user_message += f"\n\nINPUT_JSON:\n{input_str}"

    if schema:
        schema_str = json.dumps(schema, indent=2)
        user_message += f"\n\nOUTPUT_SCHEMA (your response MUST match this):\n{schema_str}"

    try:
        result = engine.single_shot(
            system_prompt=JSON_SYSTEM_PROMPT,
            user_message=user_message,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        return {"ok": False, "error": f"LLM call failed: {e}"}

    if not result or not result.strip():
        return {"ok": False, "error": "LLM returned empty response"}

    cleaned = _strip_code_fences(result)

    if not cleaned or not cleaned.lstrip().startswith(("{", "[")):
        return {"ok": False, "error": "LLM did not return JSON", "raw": cleaned[:500]}

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Invalid JSON from LLM: {e}", "raw": cleaned[:500]}

    if schema:
        valid, err = _validate_schema(data, schema)
        if not valid:
            return {
                "ok": False,
                "error": f"Schema validation failed: {err}",
                "data": data,
            }

    return {"ok": True, "data": data}


def build_llm_task_tools(engine=None):
    """Build the llm_task tool for Ghost's tool registry."""

    def llm_task_exec(prompt, input_json=None, schema=None, max_tokens=2048):
        if engine is None:
            return "Error: LLM engine not available"

        if isinstance(input_json, str):
            try:
                input_json = json.loads(input_json)
            except json.JSONDecodeError:
                return f"Error: input_json is not valid JSON"

        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except json.JSONDecodeError:
                return f"Error: schema is not valid JSON"

        result = run_llm_task(
            engine=engine,
            prompt=prompt,
            input_json=input_json,
            schema=schema,
            max_tokens=max_tokens,
        )

        if result["ok"]:
            return json.dumps({
                "status": "ok",
                "data": result["data"],
            }, indent=2, default=str)
        else:
            return json.dumps({
                "status": "error",
                "error": result["error"],
                "raw": result.get("raw", ""),
            }, indent=2, default=str)

    return [
        {
            "name": "llm_task",
            "description": (
                "Execute a structured JSON-only LLM subtask. The LLM responds with "
                "pure JSON (no tools, no markdown). Optionally provide input data and "
                "a JSON Schema for validation. Useful for: data extraction, classification, "
                "structured summaries, entity extraction, sentiment analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Task instruction — what to extract/classify/generate",
                    },
                    "input_json": {
                        "type": "object",
                        "description": "Optional input data as JSON object",
                    },
                    "schema": {
                        "type": "object",
                        "description": "Optional JSON Schema that the output must match",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Max tokens for the response",
                        "default": 2048,
                    },
                },
                "required": ["prompt"],
            },
            "execute": llm_task_exec,
        },
    ]
