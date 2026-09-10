import asyncio
import unittest

from pydantic import ValidationError
from pydantic_ai import Agent, CallDeferred, DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from geoai.server.state import _latest_plan_items
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


if __name__ == "__main__":
    unittest.main()
