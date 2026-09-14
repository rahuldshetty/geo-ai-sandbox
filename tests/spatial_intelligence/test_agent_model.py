"""Model resolution: a custom OpenAI-compatible endpoint and the API it speaks."""

import os
import unittest
from unittest.mock import patch

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel

from spatial_intelligence.agent.model import resolve_model

BASE_URL = "http://127.0.0.1:8080/v1"


def endpoint(base_url: str = BASE_URL):
    """Patch the process environment for one endpoint, with no API key set."""
    return patch.dict(os.environ, {"OPENAI_BASE_URL": base_url, "OPENAI_API_KEY": ""})


class ResolveModelTests(unittest.TestCase):
    """`OPENAI_BASE_URL` binds an OpenAI kind to the endpoint and picks its API."""

    def test_a_bare_openai_model_speaks_chat_completions(self):
        # The registration of the llama.cpp/DeepSeek-class failure: pydantic-ai
        # would infer the Responses API for `openai:`, which such a server does
        # not implement compatibly.
        with endpoint():
            model = resolve_model("openai:qwen3")

        self.assertIsInstance(model, OpenAIChatModel)
        self.assertEqual(model.model_name, "qwen3")
        self.assertEqual(str(model.client.base_url).rstrip("/"), BASE_URL)

    def test_openai_chat_names_the_same_api(self):
        with endpoint():
            model = resolve_model("openai-chat:qwen3")

        self.assertIsInstance(model, OpenAIChatModel)
        self.assertEqual(str(model.client.base_url).rstrip("/"), BASE_URL)

    def test_openai_responses_asks_for_the_responses_api(self):
        with endpoint():
            model = resolve_model("openai-responses:gpt-5")

        self.assertIsInstance(model, OpenAIResponsesModel)
        self.assertEqual(str(model.client.base_url).rstrip("/"), BASE_URL)

    def test_the_key_is_optional_for_a_custom_endpoint(self):
        # An empty `.env` line counts as *set* in the environment, which defeats the
        # placeholder pydantic-ai substitutes for an absent key: the client must
        # still be buildable, or a keyless local endpoint would fail at request time.
        with endpoint():
            model = resolve_model("openai-chat:qwen3")

        self.assertTrue(model.client.api_key)

    def test_a_full_chat_completions_url_is_trimmed_to_the_base(self):
        with endpoint(f"{BASE_URL}/chat/completions"):
            model = resolve_model("openai:qwen3")

        self.assertEqual(str(model.client.base_url).rstrip("/"), BASE_URL)

    def test_other_providers_and_a_ready_made_model_pass_through(self):
        with endpoint():
            self.assertEqual(resolve_model("anthropic:claude-sonnet-4-5"), "anthropic:claude-sonnet-4-5")
            self.assertEqual(resolve_model("ollama:llama3.1"), "ollama:llama3.1")
            test_model = TestModel()
            self.assertIs(resolve_model(test_model), test_model)

    def test_without_a_base_url_the_string_is_left_to_pydantic_ai(self):
        with endpoint(""):
            self.assertEqual(resolve_model("openai:gpt-4o"), "openai:gpt-4o")
            self.assertEqual(resolve_model("openai-chat:qwen3"), "openai-chat:qwen3")
            self.assertEqual(resolve_model("openai-responses:gpt-5"), "openai-responses:gpt-5")
