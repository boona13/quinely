"""
ghost_edit_tools.py — Precise, surgical file-editing tools.

Adds the editing primitives that real coding agents rely on, complementing the
whole-file ``file_write`` tool:

  • ``edit_file``  — exact string search-and-replace (the StrReplace pattern).
                     Requires the target snippet to be unique unless
                     ``replace_all`` is set, so edits are unambiguous.
  • ``apply_patch`` — apply a standard unified diff (``@@`` hunks) to a file,
                     locating each hunk by its context lines (with a small
                     positional fallback) instead of trusting line numbers.

Both tools reuse Ghost's existing safety guards: they refuse to modify Ghost's
own source tree (self-modification must go through the evolve pipeline) and
auto-register artifacts / projects exactly like ``file_write``.

This module is intentionally dependency-free (pure stdlib) so it works on
macOS, Linux, and Windows without any system packages.
"""

import difflib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ghost_tools import (
    PROJECT_DIR,
    DEFAULT_ALLOWED_ROOTS,
    _check_path_allowed,
    _is_ghost_codebase_path,
    _auto_register_project,
)

MAX_PREVIEW_LINES = 40


# ─────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────

def _resolve_target(path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a user-supplied path to an existing file.

    Returns (path, None) on success or (None, error_message) on failure.
    Bare filenames fall back to the Ghost project root so relative references
    behave like ``file_read``.
    """
    stripped = (path or "").strip()
    if not stripped or len(stripped) < 2:
        return None, f"ERROR: Invalid path '{path}'. Provide a full file path."

    p = Path(stripped).expanduser()
    if not p.exists():
        # Fall back to project root for bare names (mirrors file_read behaviour).
        candidate = PROJECT_DIR / Path(stripped).name if len(p.parts) <= 1 else PROJECT_DIR / stripped
        if candidate.exists():
            p = candidate
        else:
            return None, f"File not found: {path} (also tried {candidate})"
    if not p.is_file():
        return None, f"Not a file: {path}"
    return p, None


def _guard(path: str, p: Path, allowed_roots) -> Optional[str]:
    """Return an error string if the path may not be edited, else None."""
    if not _check_path_allowed(str(p), allowed_roots):
        return f"DENIED: Path '{path}' is outside allowed roots"
    if _is_ghost_codebase_path(str(p)):
        return (
            f"BLOCKED: '{path}' is inside Ghost's own codebase ({PROJECT_DIR}). "
            "Direct edits to Ghost source are not allowed from this context — "
            "use the evolution pipeline (evolve_plan / evolve_apply) for "
            "self-modification. For user project files, pass a path outside the "
            "Ghost install directory."
        )
    return None


def _post_write(p: Path) -> None:
    """Run the same side effects file_write performs after a successful write."""
    try:
        from ghost_artifacts import auto_register
        auto_register(str(p))
    except Exception:
        pass
    try:
        _auto_register_project(p)
    except Exception:
        pass


def _diff_preview(before: str, after: str, path: str) -> str:
    """Produce a compact unified-diff preview of a change."""
    diff = list(difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
        n=2,
    ))
    if len(diff) > MAX_PREVIEW_LINES:
        diff = diff[:MAX_PREVIEW_LINES] + [f"... ({len(diff) - MAX_PREVIEW_LINES} more diff lines)"]
    return "\n".join(diff)


# ─────────────────────────────────────────────────────
#  edit_file — exact string replacement
# ─────────────────────────────────────────────────────

def _edit_file(path: str, old_string: str, new_string: str,
               replace_all: bool, allowed_roots) -> str:
    p, err = _resolve_target(path)
    if err:
        return err
    guard = _guard(path, p, allowed_roots)
    if guard:
        return guard

    if old_string == new_string:
        return "ERROR: old_string and new_string are identical — nothing to change."
    if not old_string:
        return (
            "ERROR: old_string is empty. To create a new file use file_write; "
            "edit_file only modifies existing content."
        )

    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Read error: {e}"

    count = text.count(old_string)
    if count == 0:
        return (
            f"ERROR: old_string not found in {p.name}. The text must match exactly "
            "(including whitespace and indentation). Read the file first to copy the "
            "exact snippet."
        )
    if count > 1 and not replace_all:
        return (
            f"ERROR: old_string appears {count} times in {p.name} and is not unique. "
            "Add more surrounding context to target a single occurrence, or set "
            "replace_all=true to replace every occurrence."
        )

    new_text = text.replace(old_string, new_string)
    try:
        p.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return f"Write error: {e}"

    _post_write(p)
    n = count if replace_all else 1
    preview = _diff_preview(text, new_text, p.name)
    body = f"OK: replaced {n} occurrence{'s' if n != 1 else ''} in {p}"
    if preview:
        body += "\n\n" + preview
    return body


# ─────────────────────────────────────────────────────
#  apply_patch — unified-diff application
# ─────────────────────────────────────────────────────

class _Hunk:
    __slots__ = ("old_start", "old_lines", "new_lines")

    def __init__(self, old_start: int):
        self.old_start = old_start          # 1-based line from the @@ header
        self.old_lines: List[str] = []      # context + deletions ("before")
        self.new_lines: List[str] = []      # context + additions ("after")


def _parse_hunks(patch: str) -> Tuple[List[_Hunk], Optional[str]]:
    """Parse a unified diff into a list of hunks. File headers are ignored —
    the target file is supplied explicitly to apply_patch."""
    hunks: List[_Hunk] = []
    current: Optional[_Hunk] = None
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            # Format: @@ -l,s +l,s @@ optional section heading
            try:
                seg = raw.split("@@")[1].strip()
                minus = [t for t in seg.split() if t.startswith("-")][0]
                old_start = int(minus[1:].split(",")[0])
            except Exception:
                old_start = 1
            current = _Hunk(old_start)
            hunks.append(current)
            continue
        if current is None:
            # Skip diff/file headers (---, +++, diff --git, index ...) before first hunk.
            continue
        if raw.startswith("+"):
            current.new_lines.append(raw[1:])
        elif raw.startswith("-"):
            current.old_lines.append(raw[1:])
        elif raw.startswith(" "):
            current.old_lines.append(raw[1:])
            current.new_lines.append(raw[1:])
        elif raw == "":
            # A truly blank line in the diff body counts as unchanged context.
            current.old_lines.append("")
            current.new_lines.append("")
        elif raw.startswith("\\"):
            # "\ No newline at end of file" — ignore.
            continue
        else:
            # Unexpected line; treat as context to be lenient.
            current.old_lines.append(raw)
            current.new_lines.append(raw)
    if not hunks:
        return [], "ERROR: no @@ hunks found in patch. Provide a unified diff."
    return hunks, None


def _locate(lines: List[str], block: List[str], expected_idx: int) -> int:
    """Find the index where ``block`` occurs in ``lines``.

    Tries the expected position first, then scans outward, preferring the match
    nearest the expected line. Returns -1 if not found."""
    if not block:
        return max(0, min(expected_idx, len(lines)))
    n = len(block)
    limit = len(lines) - n
    if limit < 0:
        return -1
    # Exact position first.
    if 0 <= expected_idx <= limit and lines[expected_idx:expected_idx + n] == block:
        return expected_idx
    # Search outward from the expected index for the closest match.
    candidates = sorted(range(0, limit + 1), key=lambda i: abs(i - expected_idx))
    for i in candidates:
        if lines[i:i + n] == block:
            return i
    return -1


def _apply_patch(path: str, patch: str, allowed_roots) -> str:
    p, err = _resolve_target(path)
    if err:
        return err
    guard = _guard(path, p, allowed_roots)
    if guard:
        return guard

    hunks, perr = _parse_hunks(patch)
    if perr:
        return perr

    try:
        original = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Read error: {e}"

    trailing_newline = original.endswith("\n")
    lines = original.split("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines.pop()  # split() leaves a trailing "" for files ending in \n

    # Apply hunks top-to-bottom, tracking the running line offset.
    offset = 0
    applied = 0
    for idx, h in enumerate(hunks, 1):
        expected = max(0, h.old_start - 1 + offset)
        loc = _locate(lines, h.old_lines, expected)
        if loc < 0:
            ctx = " | ".join(h.old_lines[:3]) or "(empty)"
            return (
                f"ERROR: hunk #{idx} did not match {p.name}. Could not find the "
                f"context to patch near line {h.old_start}. Context tried: {ctx}. "
                "Re-read the file and regenerate the diff."
            )
        lines[loc:loc + len(h.old_lines)] = h.new_lines
        offset += len(h.new_lines) - len(h.old_lines)
        applied += 1

    new_text = "\n".join(lines)
    if trailing_newline:
        new_text += "\n"
    if new_text == original:
        return "No changes: patch produced identical content."

    try:
        p.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return f"Write error: {e}"

    _post_write(p)
    preview = _diff_preview(original, new_text, p.name)
    body = f"OK: applied {applied} hunk{'s' if applied != 1 else ''} to {p}"
    if preview:
        body += "\n\n" + preview
    return body


# ─────────────────────────────────────────────────────
#  TOOL BUILDER
# ─────────────────────────────────────────────────────

def build_edit_tools(cfg: dict = None) -> List[Dict[str, Any]]:
    """Build the precise-editing tools for the Ghost tool registry."""
    cfg = cfg or {}
    allowed_roots = cfg.get("allowed_roots", DEFAULT_ALLOWED_ROOTS)

    def edit_file_execute(path, old_string, new_string, replace_all=False):
        return _edit_file(path, old_string, new_string, bool(replace_all), allowed_roots)

    def apply_patch_execute(path, patch):
        if not patch:
            return "ERROR: patch is required (a unified diff with @@ hunks)."
        return _apply_patch(path, patch, allowed_roots)

    return [
        {
            "name": "edit_file",
            "description": (
                "Make a precise, surgical edit to an existing file by replacing an "
                "exact snippet of text. Strongly preferred over file_write for "
                "changing part of a file — you do not rewrite the whole file. "
                "old_string must match the file content EXACTLY (including whitespace "
                "and indentation) and must be UNIQUE; include a few surrounding lines "
                "for context. Set replace_all=true to replace every occurrence (e.g. "
                "renaming a variable). Cannot edit Ghost's own source — use the "
                "evolution pipeline for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or ~ path to the existing file"},
                    "old_string": {"type": "string", "description": "Exact text to replace (must be unique unless replace_all=true)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring a unique match", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
            "execute": edit_file_execute,
        },
        {
            "name": "apply_patch",
            "description": (
                "Apply a standard unified diff (with @@ hunks) to a single existing "
                "file. Each hunk is located by its context lines, so exact line "
                "numbers do not need to be perfect. Use this for multi-location edits "
                "in one call; use edit_file for a single snippet replacement. The "
                "patch body uses ' ' for context, '-' for removed, and '+' for added "
                "lines. Cannot edit Ghost's own source — use the evolution pipeline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or ~ path to the file to patch"},
                    "patch": {"type": "string", "description": "Unified diff text containing one or more @@ hunks"},
                },
                "required": ["path", "patch"],
            },
            "execute": apply_patch_execute,
        },
    ]
