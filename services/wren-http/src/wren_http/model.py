from __future__ import annotations

from typing import Any

from pydantic_ai.models import Model, ModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .settings import ServiceSettings


def build_bailian_model(settings: ServiceSettings) -> Model | None:
    """Build the one OpenAI-compatible model used by the Wren Agent."""

    if settings.model_api_key is None:
        return None
    provider = OpenAIProvider(
        base_url=settings.model_base_url.rstrip("/"),
        api_key=settings.model_api_key.get_secret_value(),
    )
    model_settings: ModelSettings = {
        "temperature": 0,
        "timeout": settings.model_timeout_seconds,
        "parallel_tool_calls": False,
        "extra_body": {"enable_thinking": False},
    }
    return OpenAIChatModel(
        settings.model_name,
        provider=provider,
        settings=model_settings,
    )


def model_name(model: Any) -> str:
    """Return a stable model name without depending on provider internals."""

    value = getattr(model, "model_name", None)
    return value if isinstance(value, str) and value else type(model).__name__
