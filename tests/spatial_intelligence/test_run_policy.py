"""Run policy: transient-failure classification, backoff, plan snapshots.

These are the decisions the run loop makes between attempts. They used to be
module-private helpers beside a hardcoded replay list; here they are the public
seams `spatial_intelligence.agent.runner` exposes, and the replay decision is
derived from the registry rather than restated.
"""

import unittest

from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelResponse, ToolCallPart

from spatial_intelligence.agent.capabilities import (
    NormalizeDuplicateToolNames,
    deduplicate_tool_name,
)
from spatial_intelligence.agent.runner import (
    MAX_BACKOFF_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    is_transient_run_error,
    latest_plan_items,
    retry_delay,
)


class TransientErrorTests(unittest.TestCase):
    def test_provider_and_transport_failures_are_retryable(self):
        self.assertTrue(is_transient_run_error(ModelAPIError("model", "connection reset")))
        self.assertTrue(is_transient_run_error(ModelHTTPError(429, "model")))
        self.assertTrue(is_transient_run_error(ModelHTTPError(408, "model")))
        self.assertTrue(is_transient_run_error(ModelHTTPError(503, "model")))
        self.assertTrue(is_transient_run_error(ModelHTTPError(500, "model")))

    def test_client_errors_and_application_bugs_are_not_retryable(self):
        self.assertFalse(is_transient_run_error(ModelHTTPError(401, "model")))
        self.assertFalse(is_transient_run_error(ModelHTTPError(400, "model")))
        self.assertFalse(is_transient_run_error(UnexpectedModelBehavior("bad tool name")))
        self.assertFalse(is_transient_run_error(ValueError("application failure")))


class RetryDelayTests(unittest.TestCase):
    def test_backoff_grows_and_is_capped(self):
        error = ModelAPIError("model", "reset")

        self.assertEqual(retry_delay(error, 1), 0.5)
        self.assertEqual(retry_delay(error, 2), 1.0)
        self.assertEqual(retry_delay(error, 3), 2.0)
        self.assertEqual(retry_delay(error, 20), MAX_BACKOFF_SECONDS)

    def test_a_provider_retry_after_is_honoured_but_bounded(self):
        self.assertEqual(
            retry_delay(ModelHTTPError(429, "model", headers={"Retry-After": "2"}), 5), 2.0
        )
        self.assertEqual(
            retry_delay(ModelHTTPError(429, "model", headers={"Retry-After": "45"}), 1),
            MAX_RETRY_AFTER_SECONDS,
        )


class PlanSnapshotTests(unittest.TestCase):
    def test_the_latest_plan_snapshot_is_restored(self):
        steps = [
            {"type": "plan", "items": [{"id": "old", "content": "Old task"}]},
            {"type": "text", "content": "working"},
            {
                "type": "plan",
                "items": [
                    {"id": "current", "content": "Load selected scenes", "status": "in_progress"}
                ],
            },
        ]

        restored = latest_plan_items(steps)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].id, "current")
        self.assertEqual(restored[0].status.value, "in_progress")

    def test_no_plan_step_means_an_empty_snapshot(self):
        self.assertEqual(latest_plan_items([{"type": "text", "content": "hi"}]), [])
        self.assertEqual(latest_plan_items([]), [])

    def test_a_malformed_snapshot_does_not_raise(self):
        steps = [{"type": "plan", "items": [{"nonsense": True}, {"id": "ok", "content": "Fine"}]}]

        restored = latest_plan_items(steps)

        self.assertEqual([item.id for item in restored], ["ok"])

    def test_a_non_list_snapshot_restores_nothing(self):
        self.assertEqual(latest_plan_items([{"type": "plan", "items": "nope"}]), [])


class ToolNameNormalizationTests(unittest.TestCase):
    def test_only_a_doubled_name_is_collapsed(self):
        self.assertEqual(deduplicate_tool_name("fit_bounds__fit_bounds"), "fit_bounds")
        self.assertEqual(deduplicate_tool_name("fit_bounds"), "fit_bounds")
        self.assertEqual(deduplicate_tool_name("a__b"), "a__b")
        self.assertEqual(deduplicate_tool_name("__x"), "__x")

    def test_the_capability_rewrites_doubled_names_before_dispatch(self):
        import asyncio

        response = ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="add_raster__add_raster",
                    args={"path": "a.tif", "name": "A"},
                    tool_call_id="call-1",
                )
            ]
        )

        normalized = asyncio.run(
            NormalizeDuplicateToolNames().after_model_request(
                None, request_context=None, response=response
            )
        )

        self.assertEqual(normalized.parts[0].tool_name, "add_raster")


if __name__ == "__main__":
    unittest.main()
