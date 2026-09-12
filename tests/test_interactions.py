import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError
from pydantic_ai import (
    Agent,
    CallDeferred,
    DeferredToolRequests,
    DeferredToolResults,
    ModelHTTPError,
)
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import geoai.agent as agent_module
import geoai.server.state as state_module
from geoai.server.state import _latest_plan_items, _ordered_notebook_cells
from geoai.skills.interaction_tools import InteractionField, request_user_input


class InteractionToolTests(unittest.TestCase):
    def test_latest_plan_snapshot_can_restore_a_resumed_run(self):
        steps = [
            {"type": "plan", "items": [{"id": "old", "content": "Old task"}]},
            {"type": "text", "content": "working"},
            {
                "type": "plan",
                "items": [
                    {
                        "id": "current",
                        "content": "Load selected scenes",
                        "status": "in_progress",
                    }
                ],
            },
        ]

        restored = _latest_plan_items(steps)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].id, "current")
        self.assertEqual(restored[0].status.value, "in_progress")

    def test_provenance_is_saved_after_parent_without_becoming_a_visible_cell(self):
        prompt = {"id": "prompt-1"}
        second_prompt = {"id": "prompt-2"}
        tool = {
            "id": "tool-1",
            "metadata": {
                "geoai": {
                    "generated": True,
                    "parent_cell_id": "prompt-1",
                }
            },
        }

        ordered = _ordered_notebook_cells([prompt, second_prompt], [tool])

        self.assertEqual(
            [cell["id"] for cell in ordered],
            ["prompt-1", "tool-1", "prompt-2"],
        )

    def test_choice_field_requires_options(self):
        with self.assertRaises(ValidationError):
            InteractionField(id="scene", label="Scene", type="radio")

    def test_request_is_deferred_with_form_metadata(self):
        field = InteractionField(
            id="scene",
            label="Scene",
            type="radio",
            options=[
                {
                    "value": "post",
                    "label": "Post-event",
                    "recommended": True,
                }
            ],
        )

        with self.assertRaises(CallDeferred) as raised:
            request_user_input("Choose imagery", "Pick a scene.", [field])

        form = raised.exception.metadata["interaction"]
        self.assertEqual(form["fields"][0]["id"], "scene")
        self.assertTrue(form["fields"][0]["options"][0]["recommended"])

    def test_agent_can_resume_with_structured_answers(self):
        def model(messages, info):
            if len(messages) > 1:
                return ModelResponse(parts=[TextPart("Loaded selected imagery.")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "request_user_input",
                        {
                            "title": "Choose imagery",
                            "prompt": "Pick a scene.",
                            "fields": [
                                {
                                    "id": "scene",
                                    "label": "Scene",
                                    "type": "radio",
                                    "options": [{"value": "post", "label": "Post-event"}],
                                }
                            ],
                        },
                    )
                ]
            )

        agent = Agent(
            FunctionModel(model),
            output_type=[str, DeferredToolRequests],
        )
        agent.tool_plain(request_user_input)

        async def run_scenario():
            paused = await agent.run("Load flood imagery")
            self.assertIsInstance(paused.output, DeferredToolRequests)
            call_id = paused.output.calls[0].tool_call_id
            return await agent.run(
                message_history=paused.all_messages(),
                deferred_tool_results=DeferredToolResults(
                    calls={call_id: {"scene": "post"}}
                ),
            )

        resumed = asyncio.run(run_scenario())
        self.assertEqual(resumed.output, "Loaded selected imagery.")


_FORM_ARGS = {
    "title": "Choose imagery",
    "prompt": "Pick a scene.",
    "fields": [
        {
            "id": "scene",
            "label": "Scene",
            "type": "radio",
            "options": [{"value": "post", "label": "Post-event", "recommended": True}],
        }
    ],
}


async def _interactive_stream(messages, info):  # noqa: ARG001 - FunctionModel contract
    """Ask for input once, then answer after the deferred result arrives."""
    for message in messages:
        for part in getattr(message, "parts", []):
            if (
                isinstance(part, ToolReturnPart)
                and part.tool_name == "request_user_input"
            ):
                yield "Loaded selected imagery."
                return
    yield {0: DeltaToolCall(name="request_user_input", json_args=json.dumps(_FORM_ARGS))}


class PromptInteractionFlowTests(unittest.TestCase):
    """Server-level pause/resume: the flow behind the browser interaction form."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="geoai-test-ws-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _wait_for_status(self, app, cell_id, statuses, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cell = next(c for c in app.cells if c["id"] == cell_id)
            if cell["status"] in statuses:
                return cell
            time.sleep(0.05)
        self.fail(f"cell stuck in {cell['status']!r}, expected one of {statuses}")

    def _run_retry_scenario(self, tool_name, args):
        model_requests = 0

        async def stream(messages, info):  # noqa: ARG001 - FunctionModel contract
            nonlocal model_requests
            model_requests += 1
            if model_requests == 1:
                yield {
                    0: DeltaToolCall(name=tool_name, json_args=json.dumps(args))
                }
            elif model_requests == 2:
                raise ModelHTTPError(503, "test-model")
            else:
                yield "Recovered after transient provider failure."

        async def no_sleep(delay):  # noqa: ARG001 - avoid backoff in tests
            return None

        with (
            mock.patch.object(
                agent_module,
                "resolve_model",
                lambda model: FunctionModel(stream_function=stream),
            ),
            mock.patch.object(
                agent_module, "_CORE_TOOLS", agent_module._CORE_TOOLS | {tool_name}
            ),
            mock.patch.object(state_module, "workspace_root", lambda name: self.tmp / name),
            mock.patch.object(state_module.asyncio, "sleep", no_sleep),
        ):
            app = state_module.AppState()
            app.new_workspace(f"retry-{tool_name}")
            cell = app.add_cell("prompt", "Run retry scenario")
            app.run_cell(cell["id"])
            finished = self._wait_for_status(
                app, cell["id"], {"done", "error", "stopped"}
            )
        return finished, model_requests

    def test_prompt_run_pauses_for_input_and_resumes_with_answers(self):
        with (
            mock.patch.object(
                agent_module,
                "resolve_model",
                lambda model: FunctionModel(stream_function=_interactive_stream),
            ),
            mock.patch.object(state_module, "workspace_root", lambda name: self.tmp / name),
        ):
            app = state_module.AppState()
            app.new_workspace("interaction-flow")
            cell = app.add_cell("prompt", "Load flood imagery")

            app.run_cell(cell["id"])
            waiting = self._wait_for_status(app, cell["id"], {"waiting_for_input"})

            self.assertIsInstance(waiting.get("interaction"), dict)
            self.assertEqual(waiting["interaction"]["title"], "Choose imagery")
            self.assertEqual(waiting["interaction"]["fields"][0]["id"], "scene")

            app.respond_interaction(
                cell["id"], waiting["interaction"]["id"], {"scene": "post"}
            )
            done = self._wait_for_status(app, cell["id"], {"done", "error", "stopped"})

            self.assertEqual(done["status"], "done")
            self.assertEqual(done["outputs"][0]["text"], "Loaded selected imagery.")
            self.assertIsNone(done.get("interaction"))
            self.assertEqual(done["interaction_history"][0]["answers"], {"scene": "post"})
            # The resumed run re-emits the deferred call; the trace must not
            # grow a duplicate (permanently pending) tool_call entry.
            calls = [s for s in done["trace"] if s.get("type") == "tool_call"]
            self.assertEqual(len(calls), 1)

    def test_transient_failure_after_read_only_tool_retries(self):
        finished, requests = self._run_retry_scenario("describe_map", {})

        self.assertEqual(finished["status"], "done")
        self.assertEqual(requests, 3)

    def test_transient_retry_restores_plan_from_before_attempt(self):
        finished, requests = self._run_retry_scenario(
            "write_plan",
            {
                "items": [
                    {
                        "content": "Temporary failed-attempt plan",
                        "status": "in_progress",
                    }
                ]
            },
        )

        self.assertEqual(finished["status"], "done")
        self.assertEqual(requests, 3)
        plan_items = asyncio.run(agent_module.current_plan_store().get_items())
        self.assertEqual(plan_items, [])

    def test_transient_failure_after_successful_mutation_does_not_replay(self):
        finished, requests = self._run_retry_scenario(
            "set_basemap",
            {"basemap": "https://tiles.openfreemap.org/styles/liberty"},
        )

        self.assertEqual(finished["status"], "error")
        self.assertEqual(requests, 2)
        self.assertIn(
            "automatic replay was skipped", finished["outputs"][0]["evalue"]
        )

    def test_transient_failure_after_failed_mutation_retries(self):
        finished, requests = self._run_retry_scenario(
            "add_raster", {"path": "data/missing.tif", "name": "Missing"}
        )

        self.assertEqual(finished["status"], "done")
        self.assertEqual(requests, 3)


if __name__ == "__main__":
    unittest.main()
