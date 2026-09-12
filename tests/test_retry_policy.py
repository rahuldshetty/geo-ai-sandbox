import asyncio
import unittest

from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelResponse, ToolCallPart

from geoai.agent import NormalizeDuplicateToolNames
from geoai.server.state import (
    _is_transient_run_error,
    _retry_delay,
    _tool_may_change_state,
)


class RetryPolicyTests(unittest.TestCase):
    def test_only_transient_provider_errors_are_retryable(self):
        self.assertTrue(_is_transient_run_error(ModelAPIError("model", "connection reset")))
        self.assertTrue(_is_transient_run_error(ModelHTTPError(429, "model")))
        self.assertTrue(_is_transient_run_error(ModelHTTPError(503, "model")))

        self.assertFalse(_is_transient_run_error(ModelHTTPError(401, "model")))
        self.assertFalse(_is_transient_run_error(UnexpectedModelBehavior("bad tool name")))
        self.assertFalse(_is_transient_run_error(ValueError("application failure")))

    def test_replay_safe_and_state_changing_tools_are_distinguished(self):
        self.assertFalse(_tool_may_change_state("describe_map"))
        self.assertFalse(_tool_may_change_state("write_plan"))
        self.assertTrue(_tool_may_change_state("add_catalog_scene"))
        self.assertTrue(_tool_may_change_state("new_unclassified_tool"))

    def test_retry_delay_respects_bounded_retry_after(self):
        error = ModelHTTPError(429, "model", headers={"Retry-After": "45"})
        self.assertEqual(_retry_delay(error, 1), 30.0)
        self.assertEqual(_retry_delay(ModelAPIError("model", "reset"), 3), 2.0)

    def test_duplicate_tool_name_is_normalized_before_dispatch(self):
        response = ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="fit_bounds__fit_bounds",
                    args={"bounds": [1, 2, 3, 4]},
                    tool_call_id="call-1",
                )
            ]
        )

        normalized = asyncio.run(
            NormalizeDuplicateToolNames().after_model_request(
                None, request_context=None, response=response
            )
        )

        self.assertEqual(normalized.parts[0].tool_name, "fit_bounds")


if __name__ == "__main__":
    unittest.main()
