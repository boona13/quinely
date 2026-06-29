"""
Onboarding Wizard System

  - Interactive step-by-step channel configuration
  - Pre-flight checks (API key validation, webhook verification)
  - Per-channel setup steps definition
  - Dashboard wizard modal integration
  - Post-setup test message
  - CLI wizard support (ghost --setup-channel <id>)

Usage:
    if isinstance(provider, OnboardingMixin):
        steps = provider.get_setup_steps()
        result = provider.validate_step(step_id, user_input)
        provider.complete_setup(collected_config)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Callable
from enum import Enum

log = logging.getLogger("ghost.channels.onboard")


class StepType(Enum):
    TEXT_INPUT = "text"
    SECRET_INPUT = "secret"
    SELECT = "select"
    CONFIRM = "confirm"
    INFO = "info"
    ACTION = "action"


@dataclass
class SetupStep:
    """A single step in the onboarding wizard."""
    id: str
    label: str
    description: str = ""
    step_type: StepType = StepType.TEXT_INPUT
    required: bool = True
    config_key: str = ""
    placeholder: str = ""
    default_value: str = ""
    options: List[Dict[str, str]] = field(default_factory=list)
    help_url: str = ""
    validation_regex: str = ""
    validation_message: str = ""
    depends_on: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "step_type": self.step_type.value,
            "required": self.required,
            "config_key": self.config_key,
        }
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.default_value:
            d["default_value"] = self.default_value
        if self.options:
            d["options"] = self.options
        if self.help_url:
            d["help_url"] = self.help_url
        if self.validation_regex:
            d["validation_regex"] = self.validation_regex
            d["validation_message"] = self.validation_message
        if self.depends_on:
            d["depends_on"] = self.depends_on
        return d


@dataclass
class StepValidation:
    """Result of validating a setup step."""
    ok: bool
    message: str = ""
    warning: str = ""


@dataclass
class SetupResult:
    """Result of completing the full setup."""
    ok: bool
    channel_id: str = ""
    message: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    test_sent: bool = False


class OnboardingMixin:
    """Mixin for ChannelProvider subclasses with onboarding wizard support.

    Override get_setup_steps() and validate_step() for channel-specific
    setup flows.
    """

    def get_setup_steps(self) -> List[SetupStep]:
        """Return the ordered list of setup steps for this channel.

        Override per channel. The default generates steps from
        get_config_schema() if available.
        """
        if hasattr(self, "get_config_schema"):
            schema = self.get_config_schema()
            steps = []
            for key, spec in schema.items():
                step_type = StepType.SECRET_INPUT if spec.get("sensitive") else StepType.TEXT_INPUT
                steps.append(SetupStep(
                    id=key,
                    label=key.replace("_", " ").title(),
                    description=spec.get("description", ""),
                    step_type=step_type,
                    required=spec.get("required", False),
                    config_key=key,
                    placeholder=spec.get("placeholder", ""),
                ))
            return steps
        return []

    def validate_step(self, step_id: str,
                       user_input: str) -> StepValidation:
        """Validate a single setup step's input.

        Override per channel for API-backed validation
        (e.g., test bot token against API).
        """
        import re
        steps = self.get_setup_steps()
        step = next((s for s in steps if s.id == step_id), None)
        if not step:
            return StepValidation(ok=False, message=f"Unknown step: {step_id}")

        if step.required and not user_input.strip():
            return StepValidation(ok=False, message=f"{step.label} is required")

        if step.validation_regex and user_input:
            if not re.match(step.validation_regex, user_input):
                msg = step.validation_message or f"Invalid format for {step.label}"
                return StepValidation(ok=False, message=msg)

        return StepValidation(ok=True)

    def complete_setup(self, collected_config: Dict[str, Any]) -> SetupResult:
        """Finalize setup with collected config.

        Calls configure() and optionally sends a test message.
        """
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        collected_config["enabled"] = True

        try:
            ok = self.configure(collected_config) if hasattr(self, "configure") else False
        except Exception as exc:
            return SetupResult(
                ok=False, channel_id=channel_id,
                message=f"Configuration failed: {exc}",
                config=collected_config,
            )

        if not ok:
            return SetupResult(
                ok=False, channel_id=channel_id,
                message="configure() returned False — check credentials",
                config=collected_config,
            )

        test_sent = False
        if hasattr(self, "send_text"):
            try:
                result = self.send_text(
                    to="", text="Quinely channel setup complete! This is a test message.",
                )
                test_sent = hasattr(result, "ok") and result.ok
            except Exception:
                pass

        return SetupResult(
            ok=True, channel_id=channel_id,
            message="Setup complete" + (" — test message sent!" if test_sent else ""),
            config=collected_config,
            test_sent=test_sent,
        )

    def get_setup_status(self) -> Dict[str, Any]:
        """Return current setup status for the wizard UI."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        configured = False
        if hasattr(self, "health_check"):
            try:
                h = self.health_check()
                configured = h.get("configured", False)
            except Exception:
                pass
        return {
            "channel_id": channel_id,
            "configured": configured,
            "steps_count": len(self.get_setup_steps()),
        }


def build_onboarding_tools(registry) -> list:
    """Build LLM tools for channel onboarding."""
    import json
    tools = []

    def channel_setup_wizard(channel: str, step_id: str = "",
                              value: str = "") -> str:
        prov = registry.get(channel)
        if not prov:
            available = ", ".join(registry.list_available())
            return f"Unknown channel '{channel}'. Available: {available}"

        if not isinstance(prov, OnboardingMixin):
            return (f"{channel} does not have an onboarding wizard. "
                    "Use channel_configure with a JSON config instead.")

        if not step_id:
            steps = prov.get_setup_steps()
            status = prov.get_setup_status()
            lines = [f"Setup wizard for {channel}:"]
            lines.append(f"  Status: {'configured' if status['configured'] else 'not configured'}")
            lines.append(f"  Steps ({len(steps)}):")
            for s in steps:
                req = " [required]" if s.required else ""
                lines.append(f"    {s.id}: {s.label}{req}")
                if s.description:
                    lines.append(f"      {s.description}")
            lines.append("\nProvide step_id and value to complete each step, "
                         "then call with step_id='complete' to finish.")
            return "\n".join(lines)

        if step_id == "complete":
            try:
                config = json.loads(value) if value else {}
            except json.JSONDecodeError:
                return "Provide config as JSON string for 'complete' step"
            result = prov.complete_setup(config)
            if result.ok:
                from ghost_channels import load_channels_config, save_channels_config
                all_cfg = load_channels_config()
                all_cfg[channel] = result.config
                save_channels_config(all_cfg)
                return f"OK: {result.message}"
            return f"FAILED: {result.message}"

        validation = prov.validate_step(step_id, value)
        if validation.ok:
            msg = f"OK: Step '{step_id}' validated"
            if validation.warning:
                msg += f" (warning: {validation.warning})"
            return msg
        return f"FAILED: {validation.message}"

    tools.append({
        "name": "channel_setup_wizard",
        "description": (
            "Interactive setup wizard for configuring a messaging channel. "
            "Call without step_id to see available steps. "
            "Call with step_id and value to validate each step. "
            "Call with step_id='complete' and value='{config json}' to finish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID to set up"},
                "step_id": {"type": "string",
                            "description": "Step ID to validate, or 'complete'",
                            "default": ""},
                "value": {"type": "string",
                          "description": "Value for the step, or JSON config for 'complete'",
                          "default": ""},
            },
            "required": ["channel"],
        },
        "execute": channel_setup_wizard,
    })

    return tools
