"""Human-in-the-loop tools rendered as structured forms by the web app."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_ai import CallDeferred

from ..runtime import ToolRuntime
from ..spec import ToolKind, pack, tool


class ChoiceOption(BaseModel):
    value: str
    label: str
    description: str | None = None
    recommended: bool = False
    thumbnail_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InteractionField(BaseModel):
    id: str
    label: str
    type: Literal["radio", "multi_select", "text", "confirmation"]
    description: str | None = None
    required: bool = True
    options: list[ChoiceOption] = Field(default_factory=list)
    default: Any = None
    placeholder: str | None = None

    @model_validator(mode="after")
    def validate_options(self):
        if self.type in {"radio", "multi_select"} and not self.options:
            raise ValueError(f"{self.type} field {self.id!r} requires options")
        return self


@pack(category="interaction")
class InteractionPack:
    """Asks the user a structured question by deferring the run."""

    def __init__(self, runtime: ToolRuntime) -> None:
        """The registry hands every pack its runtime; this tool needs none of it."""

    @tool(category="interaction", kind=ToolKind.INTERACTIVE, core=True)
    def request_user_input(
        self,
        title: str,
        prompt: str,
        fields: list[InteractionField],
        submit_label: str = "Continue",
        allow_cancel: bool = True,
    ) -> str:
        """Pause and ask the user for consequential or ambiguous choices.

        Use radio for exactly one choice, multi_select for several choices, text for
        custom input, and confirmation for yes/no. Include a recommended option when
        evidence supports one. Do not ask for minor reversible decisions.
        """
        form = {
            "title": title,
            "prompt": prompt,
            "fields": [field.model_dump(mode="json") for field in fields],
            "submit_label": submit_label,
            "allow_cancel": allow_cancel,
        }
        # The browser supplies this externally. Pydantic AI ends the current run
        # with DeferredToolRequests and consumes the submitted value on resume.
        raise CallDeferred(metadata={"interaction": form})


__all__ = ["ChoiceOption", "InteractionField", "InteractionPack"]
