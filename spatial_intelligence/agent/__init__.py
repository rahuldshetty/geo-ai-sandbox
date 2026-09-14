"""Agent wiring: prompt, model resolution, capabilities, and the run loop."""

from .builder import TOOL_RETRIES, BuiltAgent, build_agent
from .capabilities import (
    NormalizeDuplicateToolNames,
    ToolFailurePolicy,
    deduplicate_tool_name,
    is_transient_tool_error,
    tool_failure_text,
)
from .events import json_safe, step_from_event
from .interactions import (
    answers_markdown,
    interaction_from_deferred,
    pending_interaction,
    validate_answers,
)
from .model import resolve_model
from .prompt import SYSTEM_PROMPT
from .runner import (
    PromptRunner,
    RunOutcome,
    RunnerHooks,
    describe_run_error,
    is_transient_run_error,
    latest_plan_items,
    resume_payload,
    retry_delay,
)

__all__ = [
    "SYSTEM_PROMPT",
    "TOOL_RETRIES",
    "BuiltAgent",
    "NormalizeDuplicateToolNames",
    "PromptRunner",
    "RunOutcome",
    "RunnerHooks",
    "ToolFailurePolicy",
    "answers_markdown",
    "build_agent",
    "deduplicate_tool_name",
    "describe_run_error",
    "interaction_from_deferred",
    "is_transient_run_error",
    "is_transient_tool_error",
    "json_safe",
    "latest_plan_items",
    "pending_interaction",
    "resolve_model",
    "resume_payload",
    "retry_delay",
    "step_from_event",
    "tool_failure_text",
    "validate_answers",
]
