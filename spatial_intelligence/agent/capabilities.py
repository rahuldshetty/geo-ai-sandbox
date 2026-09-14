"""Agent capabilities: the harness hooks this app needs.

Both capabilities exist because tool failure and provider quirks must not kill
a run: a raised tool exception becomes a model-visible failure, and the
``name__name`` alias some compatible models emit is normalized before dispatch.
"""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai import ToolFailed
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart

from ..tools.runtime import ToolRuntime


class ToolErrorFeedback(AbstractCapability[ToolRuntime]):
    """Convert a raised tool exception into a model-visible failure.

    By default pydantic-ai aborts the whole run when a tool raises. Tools raise
    ordinary exceptions for recoverable conditions (a bounds value outside the
    Web Mercator range, a file too large to read whole, a missing column), so we
    surface those as a ``ToolFailed`` result instead: the model sees the error
    and adapts in the same run, mirroring how ``run_python`` returns a traceback
    string rather than raising.
    """

    async def on_tool_execute_error(self, ctx, *, call, tool_def, args, error):
        raise ToolFailed(f"{type(error).__name__}: {error}")


class NormalizeDuplicateToolNames(AbstractCapability[ToolRuntime]):
    """Correct the ``name__name`` alias emitted by some compatible models."""

    async def after_model_request(self, ctx, *, request_context, response):
        parts = [
            replace(part, tool_name=deduplicate_tool_name(part.tool_name))
            if isinstance(part, ToolCallPart)
            else part
            for part in response.parts
        ]
        return replace(response, parts=parts)


def deduplicate_tool_name(name: str) -> str:
    """Collapse ``tool__tool`` into ``tool``; leave other names untouched."""
    left, separator, right = name.partition("__")
    return left if separator and left == right else name
