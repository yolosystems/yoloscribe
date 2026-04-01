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
    provider: Literal["anthropic", "bedrock"]
    model_id: str
    supports_thinking: bool = False


MODEL_REGISTRY: dict[str, ModelSpec] = {
    # Anthropic direct
    "haiku":   ModelSpec("anthropic", "claude-haiku-4-5-20251001"),
    "sonnet":  ModelSpec("anthropic", "claude-sonnet-4-6"),
    "opus":    ModelSpec("anthropic", "claude-opus-4-6"),
    # Amazon Bedrock
    "bedrock-haiku":  ModelSpec("bedrock", "anthropic.claude-haiku-4-5-20251001-v1:0"),
    "bedrock-sonnet": ModelSpec("bedrock", "anthropic.claude-sonnet-4-6-20250514-v1:0"),
    "bedrock-opus":   ModelSpec("bedrock", "anthropic.claude-opus-4-6-20250514-v1:0"),
}

DEFAULT_MODEL_KEY = "sonnet"


def build_strands_model(model_key: str):
    """Return a strands-compatible model object for the given registry key.

    If the key is not in MODEL_REGISTRY, it is passed directly to BedrockModel
    as a model ID or inference profile ARN (e.g. arn:aws:bedrock:...).
    Falls back to DEFAULT_MODEL_KEY only if the key is empty.
    """
    spec = MODEL_REGISTRY.get(model_key)
    if spec is None:
        from strands.models.bedrock import BedrockModel
        fallback = MODEL_REGISTRY[DEFAULT_MODEL_KEY]
        model_id = model_key if model_key else fallback.model_id
        return BedrockModel(model_id=model_id)
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
