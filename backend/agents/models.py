"""Model registry for YoloScribe agents.

Usage:
    from .models import build_strands_model, resolve_model_key

    model_key = resolve_model_key("YOLOSCRIBE_WRITER_MODEL", "YOLOSCRIBE_MODEL")
    model = build_strands_model(model_key)
"""

from __future__ import annotations

import dataclasses
import os
from typing import Literal


@dataclasses.dataclass
class ModelSpec:
    provider: Literal["anthropic", "bedrock", "openai"]
    model_id: str
    supports_thinking: bool = False


MODEL_REGISTRY: dict[str, ModelSpec] = {
    # Anthropic direct
    "haiku":   ModelSpec("anthropic", "claude-haiku-4-5-20251001"),
    "sonnet":  ModelSpec("anthropic", "claude-sonnet-4-6"),
    "opus":    ModelSpec("anthropic", "claude-opus-4-6"),
    "glm":     ModelSpec("openai", "zai.glm-5"),
    # Amazon Bedrock
    "bedrock-haiku":  ModelSpec("bedrock", "anthropic.claude-haiku-4-5-20251001-v1:0"),
    "bedrock-sonnet": ModelSpec("bedrock", "anthropic.claude-sonnet-4-6-20250514-v1:0"),
    "bedrock-opus":   ModelSpec("bedrock", "anthropic.claude-opus-4-6-20250514-v1:0"),
}

DEFAULT_MODEL_KEY = "sonnet"


def _build_litellm_model(model_key: str, base_url: str):
    """Return an OpenAI-compatible Strands model pointed at the LiteLLM proxy.

    The model key is passed straight through as the OpenAI `model` — LiteLLM's
    `model_list` maps the name to a provider/model/credential. Provider branching
    and per-provider credential wiring live in the proxy config, not here.
    """
    from openai import AsyncOpenAI
    from strands.models.openai import OpenAIModel

    api_key = os.getenv("LITELLM_API_KEY", "").strip() or "sk-litellm-local"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return OpenAIModel(client=client, model_id=model_key or DEFAULT_MODEL_KEY)


def build_strands_model(model_key: str):
    """Return a strands-compatible model object for the given registry key.

    When LITELLM_BASE_URL is set, all model routing goes through the LiteLLM
    proxy (YOL-512) and the native per-provider paths below are bypassed.
    Otherwise (legacy default): if the key is not in MODEL_REGISTRY it is passed
    directly to BedrockModel as a model ID or inference profile ARN
    (e.g. arn:aws:bedrock:...); falls back to DEFAULT_MODEL_KEY only if empty.
    """
    litellm_base_url = os.getenv("LITELLM_BASE_URL", "").strip()
    if litellm_base_url:
        return _build_litellm_model(model_key, litellm_base_url)

    spec = MODEL_REGISTRY.get(model_key)
    if spec is None:
        from strands.models.bedrock import BedrockModel
        fallback = MODEL_REGISTRY[DEFAULT_MODEL_KEY]
        model_id = model_key if model_key else fallback.model_id
        return BedrockModel(model_id=model_id)
    if spec.provider == "openai":
        from openai import AsyncOpenAI
        from strands.models.openai import OpenAIModel
        from aws_bedrock_token_generator import provide_token
        base_url = os.getenv("YOLOSCRIBE_MODEL_BASE_URL", "https://bedrock-mantle.us-west-2.api.aws/v1").strip()
        client = AsyncOpenAI(api_key=provide_token(), base_url=base_url, project="default")
        return OpenAIModel(client=client, model_id=spec.model_id)
    if spec.provider == "anthropic":
        from strands.models.anthropic import AnthropicModel
        return AnthropicModel(
            model_id=spec.model_id,
            max_tokens=4096,
            client_args={"max_retries": 0},
        )
    else:
        from strands.models.bedrock import BedrockModel
        return BedrockModel(model_id=spec.model_id)


def resolve_model_key(*env_vars: str) -> str:
    """Return the first non-empty env var value, falling back to DEFAULT_MODEL_KEY.

    Usage:
        resolve_model_key("YOLOSCRIBE_WRITER_MODEL", "YOLOSCRIBE_MODEL")
    """
    for var in env_vars:
        val = os.getenv(var, "").strip()
        if val:
            return val
    return DEFAULT_MODEL_KEY
