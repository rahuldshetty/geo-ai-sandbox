"""Model resolution, so a custom OpenAI-compatible endpoint can be honored."""

from __future__ import annotations

import os


def validate_model(model: object) -> None:
    """Raise when a model string cannot be resolved to a model.

    Settings must not accept a model that would only fail later, when a
    workspace is opened — which is exactly when a rebuild becomes possible.
    A ready-made ``Model`` instance passes through untouched.
    """
    if not isinstance(model, str):
        return
    resolved = resolve_model(model)
    if not isinstance(resolved, str):
        return
    from pydantic_ai.models import infer_model

    infer_model(resolved)


def resolve_model(model: str):
    """Resolve a model string to a ``Model`` instance, honoring a custom endpoint.

    When ``OPENAI_BASE_URL`` is set and ``model`` is an ``openai:`` model, the
    model is built with an ``OpenAIProvider`` bound to that endpoint (Azure
    OpenAI, LiteLLM, vLLM, OpenRouter, other OpenAI-compatible servers). A
    caller may also pass a ready-made ``Model`` instance, which is returned as
    is. Otherwise the string is returned unchanged and pydantic-ai infers the
    provider.
    """
    if not isinstance(model, str):
        return model
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if not base_url or not model.startswith("openai:"):
        return model

    # OPENAI_BASE_URL is a base URL: pydantic-ai appends /chat/completions
    # itself. Tolerate a user-supplied full endpoint so a trailing
    # /chat/completions doesn't double-suffix into a 404.
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")].rstrip("/")

    from pydantic_ai.models import infer_model, infer_provider
    from pydantic_ai.providers.openai import OpenAIProvider

    def provider_factory(provider_name: str):
        if provider_name == "openai":
            return OpenAIProvider(base_url=base_url)
        return infer_provider(provider_name)

    return infer_model(model, provider_factory=provider_factory)
