"""
GHOST Skills System

Skills are markdown-based knowledge packs that extend Ghost's capabilities.
Each skill is a folder with a SKILL.md file containing YAML frontmatter + instructions.
Community can drop skills into ~/.ghost/skills/ with zero code.
"""

import os
import re
import yaml
import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("ghost.skills")


GHOST_HOME = Path.home() / ".ghost"
SKILLS_USER_DIR = GHOST_HOME / "skills"
SKILLS_BUNDLED_DIR = Path(__file__).resolve().parent / "skills"

SKILLS_USER_DIR.mkdir(parents=True, exist_ok=True)

# Default model aliases for per-skill model overrides
# These can be overridden via config.json skill_model_aliases
DEFAULT_MODEL_ALIASES = {
    "cheap": "openrouter/google/gemini-2.0-flash-001",
    "fast": "openrouter/google/gemini-2.0-flash-001",
    "capable": "openrouter/anthropic/claude-sonnet-4-6",
    "smart": "openrouter/anthropic/claude-opus-4-6",
    "vision": "openrouter/anthropic/claude-sonnet-4-6",
    "code": "openrouter/openai/gpt-5.5",
}


def _get_model_aliases() -> Dict[str, str]:
    """Load model aliases from config with fallback to defaults."""
    config_path = GHOST_HOME / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding='utf-8'))
            aliases = cfg.get("skill_model_aliases", {})
            if isinstance(aliases, dict) and aliases:
                # Merge with defaults (user aliases override defaults)
                merged = dict(DEFAULT_MODEL_ALIASES)
                merged.update(aliases)
                return merged
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load skill_model_aliases from config: %s", exc)
    return dict(DEFAULT_MODEL_ALIASES)


def resolve_model_alias(model: str | None) -> str | None:
    """Resolve a model alias to full provider:model format.
    
    Supports:
    - Aliases: 'cheap', 'fast', 'capable', 'smart', 'vision', 'code' (configurable)
    - Full format: 'provider/model' (passed through)
    - Provider-prefixed: 'google/gemini-2.0-flash-001' (passed through)
    """
    if not model:
        return None
    model = model.strip().lower()
    aliases = _get_model_aliases()
    if model in aliases:
        return aliases[model]
    return model


def validate_skill_model(model: str | None) -> Tuple[bool, str]:
    """Validate a skill model override.
    
    Returns (is_valid, error_message_or_normalized_model).
    """
    if not model:
        return True, ""  # No model override is valid
    
    resolved = resolve_model_alias(model)
    aliases = _get_model_aliases()
    
    # Must be in provider/model format
    if "/" not in resolved:
        return False, f"Model must be in 'provider/model' format or a known alias ({', '.join(sorted(aliases.keys()))})"
    
    parts = resolved.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False, "Model must be in 'provider/model' format"
    
    provider, model_name = parts
    
    # List of known providers
    known_providers = {
        "openrouter", "openai", "anthropic", "google",
        "ollama", "openai-codex", "deepseek"
    }
    
    if provider not in known_providers:
        return False, f"Unknown provider '{provider}'. Known: {', '.join(sorted(known_providers))}"
    
    return True, resolved


class Skill:
    """A loaded skill with metadata and instructions."""
    __slots__ = ("name", "description", "triggers", "tools",
                 "body", "path", "priority", "os_filter", "requires", "model")

    def __init__(self, name, description, triggers, tools, body, path,
                 priority=0, os_filter=None, requires=None, model=None):
        self.name = name
        self.description = description
        self.triggers = triggers or []
        self.tools = tools or []
        self.body = body
        self.path = path
        self.priority = priority
        self.os_filter = os_filter
        self.requires = requires or {}
        self.model = model

    def to_prompt_section(self):
        """Format this skill for injection into the system prompt."""
        return (
            f"<skill name=\"{self.name}\">\n"
            f"{self.body.strip()}\n"
            f"</skill>"
        )

    def __repr__(self):
        return f"Skill({self.name}, triggers={self.triggers})"


def parse_skill_md(path):
    """Parse a SKILL.md file into a Skill object."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    frontmatter = {}
    body = text

    fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.DOTALL)
    if fm_match:
        try:
            frontmatter = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError:
            frontmatter = {}
        body = fm_match.group(2)

    name = frontmatter.get("name", path.parent.name)
    description = frontmatter.get("description", "")
    raw_triggers = frontmatter.get("triggers", [])
    had_bad_triggers = False
    if isinstance(raw_triggers, str):
        triggers = [t.strip() for t in raw_triggers.split(",")]
    elif isinstance(raw_triggers, list):
        flat = []
        for t in raw_triggers:
            if isinstance(t, str):
                flat.append(t.strip())
            elif isinstance(t, dict):
                had_bad_triggers = True
                for v in t.values():
                    if isinstance(v, list):
                        flat.extend(str(x).strip() for x in v)
                    elif isinstance(v, str):
                        flat.append(v.strip())
            else:
                had_bad_triggers = True
                flat.append(str(t).strip())
        triggers = [t for t in flat if t]
    else:
        had_bad_triggers = True
        triggers = [str(raw_triggers).strip()] if raw_triggers else []
    if had_bad_triggers:
        print(f"  [SKILLS] WARNING: {path} has malformed triggers (dicts instead of strings). "
              f"Auto-flattened to: {triggers}. Fix the SKILL.md frontmatter.")
    tools = frontmatter.get("tools", [])
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",")]
    priority = frontmatter.get("priority", 0)
    # Defensive: YAML may return string if user quoted it; ensure numeric
    if isinstance(priority, str):
        try:
            priority = int(priority)
        except ValueError:
            try:
                priority = float(priority)
            except ValueError:
                priority = 0
    elif not isinstance(priority, (int, float)):
        priority = 0
    os_filter = frontmatter.get("os", None)
    requires = frontmatter.get("requires", {})
    raw_model = frontmatter.get("model", None)
    
    # Validate and normalize model override
    model = None
    if raw_model:
        is_valid, result = validate_skill_model(raw_model)
        if is_valid:
            model = result
        else:
            log.warning("Skill '%s' has invalid model '%s': %s", name, raw_model, result)
            model = None  # Invalid model, don't use it

    return Skill(
        name=name,
        description=description,
        triggers=triggers,
        tools=tools,
        body=body,
        path=str(path),
        priority=priority,
        os_filter=os_filter,
        requires=requires,
        model=model,
    )


class SkillLoader:
    """Discovers, loads, and selects skills from multiple directories via LLM."""

    def __init__(self, extra_dirs=None):
        self.skills: Dict[str, Skill] = {}
        self._dirs = []
        self._last_scan = 0

        if SKILLS_BUNDLED_DIR.is_dir():
            self._dirs.append(SKILLS_BUNDLED_DIR)
        if SKILLS_USER_DIR.is_dir():
            self._dirs.append(SKILLS_USER_DIR)
        if extra_dirs:
            for d in extra_dirs:
                p = Path(d).expanduser()
                if p.is_dir():
                    self._dirs.append(p)

        self.reload()

    def reload(self):
        """Scan all skill directories and load/refresh skills."""
        import platform
        current_os = platform.system().lower()

        loaded = {}
        for skill_dir in self._dirs:
            skill_md = skill_dir / "SKILL.md"
            if skill_md.is_file():
                skill = parse_skill_md(skill_md)
                if skill:
                    loaded[skill.name] = skill
                continue

            try:
                for sub in sorted(skill_dir.iterdir()):
                    if not sub.is_dir():
                        continue
                    skill_md = sub / "SKILL.md"
                    if skill_md.is_file():
                        skill = parse_skill_md(skill_md)
                        if skill:
                            if skill.os_filter:
                                os_list = skill.os_filter if isinstance(skill.os_filter, list) else [skill.os_filter]
                                if current_os not in [o.lower() for o in os_list]:
                                    continue
                            loaded[skill.name] = skill
            except PermissionError:
                continue

        self.skills = loaded
        self._last_scan = time.time()

    def check_reload(self, interval=30):
        """Reload if enough time has passed."""
        if time.time() - self._last_scan > interval:
            self.reload()

    def build_catalog(self, disabled=None):
        """Compact name+description list for LLM-based skill selection."""
        disabled = disabled or set()
        lines = []
        for skill in self.skills.values():
            if skill.name in disabled:
                continue
            desc = skill.description or "(no description)"
            lines.append(f"- {skill.name}: {desc}")
        return "\n".join(lines)

    def llm_match(self, engine, user_message, content_type=None, disabled=None):
        """Use LLM to select relevant skills. Falls back to [] on failure.

        1. Content-type fast path — if content_type is set and a skill has it
           in its triggers, include that skill directly (no LLM needed).
        2. LLM classification — send a compact catalog to a fast model and
           ask it to return a JSON array of relevant skill names.
        """
        disabled = disabled or set()
        if not user_message or not self.skills:
            return []

        ct_matches = []
        if content_type:
            for skill in self.skills.values():
                if skill.name in disabled:
                    continue
                if content_type in skill.triggers:
                    ct_matches.append(skill)

        catalog = self.build_catalog(disabled=disabled)
        if not catalog.strip():
            return ct_matches

        system_prompt = (
            "You are a skill router. Given a user message, decide which skills "
            "(if any) are relevant.\n\n"
            "Rules:\n"
            "- Return ONLY a JSON array of skill names, e.g. [\"trader\", \"browser\"].\n"
            "- Return [] if no skill is relevant.\n"
            "- Pick at most 2-3 skills. Fewer is better.\n"
            "- Only pick a skill if the user's intent clearly matches it.\n\n"
            f"Available skills:\n{catalog}"
        )

        try:
            result = engine.single_shot(
                system_prompt=system_prompt,
                user_message=user_message[:2000],
                temperature=0.0,
                max_tokens=200,
            )
            raw = (result.text if hasattr(result, "text") else str(result)).strip()
            bracket_start = raw.find("[")
            bracket_end = raw.rfind("]")
            if bracket_start == -1 or bracket_end == -1:
                log.debug("SkillLoader.llm_match: no JSON array in response: %s", raw[:200])
                return ct_matches
            names = json.loads(raw[bracket_start:bracket_end + 1])
            if not isinstance(names, list):
                return ct_matches
        except Exception as exc:
            log.warning("SkillLoader.llm_match LLM call failed: %s", exc)
            return ct_matches

        llm_skills = []
        for name in names:
            if not isinstance(name, str):
                continue
            skill = self.skills.get(name)
            if skill and skill.name not in disabled:
                llm_skills.append(skill)

        seen = {s.name for s in ct_matches}
        merged = list(ct_matches)
        for s in llm_skills:
            if s.name not in seen:
                merged.append(s)
                seen.add(s.name)

        def _priority_key(s):
            p = s.priority
            if isinstance(p, (int, float)):
                return -p
            return 0
        merged.sort(key=_priority_key)
        return merged

    def get(self, name):
        return self.skills.get(name)

    def list_all(self):
        return list(self.skills.values())

    def build_skills_prompt(self, matched_skills):
        """Build the skills section to inject into the system prompt."""
        if not matched_skills:
            return ""
        parts = ["\n<available_skills>"]
        for skill in matched_skills:
            parts.append(skill.to_prompt_section())
        parts.append("</available_skills>\n")
        parts.append(
            "Follow the instructions in the matched skills above. "
            "They provide specialized knowledge for handling this content."
        )
        return "\n".join(parts)

    def get_tools_for_skills(self, matched_skills):
        """Collect tool names needed by the matched skills."""
        tool_names = set()
        for skill in matched_skills:
            tool_names.update(skill.tools)
        return list(tool_names)
