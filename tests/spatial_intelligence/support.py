"""Shared test doubles for the session and server tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel


class FakeResponse:
    """A minimal stand-in for ``urllib.request.urlopen``'s response."""

    def __init__(self, body: bytes, *, content_length: str | None = None):
        self._body = body
        self._offset = 0
        self.headers = {
            "Content-Length": str(len(body)) if content_length is None else content_length
        }

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body)
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def answered_tools(messages) -> list[str]:
    """Names of the tools the conversation already has results for."""
    return [
        part.tool_name
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]


def stream_model(handler: Callable[..., Any]) -> FunctionModel:
    """Build a streaming model, which is what the run loop requires.

    Every run goes through an event-stream handler, and pydantic-ai can stream a
    ``FunctionModel`` only when it provides ``stream_function``.
    """
    return FunctionModel(stream_function=handler)


def scripted(*turns, final: str) -> FunctionModel:
    """A model that emits ``turns`` in order, then answers with ``final``.

    Each turn is consumed once the previous one has a tool result, so a script
    can walk the real protocol — including the ``search_tools`` discovery step
    that every deferred tool requires before the model may call it.
    """

    async def stream(messages, info):
        completed = len(answered_tools(messages))
        if completed >= len(turns):
            yield final
            return
        turn = turns[completed]
        if isinstance(turn, str):
            yield turn
        else:
            yield {
                0: DeltaToolCall(
                    name=turn.tool_name, json_args=json.dumps(turn.args)
                )
            }

    return stream_model(stream)


def answers_with(text: str) -> FunctionModel:
    """A model that always answers with ``text`` and calls no tool."""

    async def stream(messages, info):
        yield text

    return stream_model(stream)


def wait_for(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` elapses."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
