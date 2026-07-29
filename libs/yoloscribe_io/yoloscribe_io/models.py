"""Model building for YoloScribe agents — all routing through the LiteLLM proxy.

Provider / model / credential resolution lives in the LiteLLM config
(`infra/litellm/config.example.yaml`), not here: the model key is passed straight
through as the OpenAI ``model`` and LiteLLM's `model_list` maps it to a provider.
Point ``LITELLM_BASE_URL`` at the proxy's OpenAI-compatible endpoint
(e.g. ``http://litellm:4000/v1``); ``LITELLM_API_KEY`` is the key presented to it.

``strands`` and ``openai`` are imported lazily so importing this module stays
cheap for consumers of the rest of ``yoloscribe_io``; the agent runtimes that
actually build models (backend, agent-runner) depend on both.
"""

from __future__ import annotations

import os

# Fallback when no per-agent-type key is selected. The valid keys are whatever
# the LiteLLM config defines as `model_name`s (see infra/litellm/config.example.yaml).
DEFAULT_MODEL_KEY = "sonnet"


def resolve_model_key(*env_vars: str) -> str:
    """Return the first non-empty env var value, else DEFAULT_MODEL_KEY.

    The per-agent-type model *policy* (which agent prefers which key) stays in
    YoloScribe; the key→provider *mechanics* live in the LiteLLM config.

    Usage: ``resolve_model_key("YOLOSCRIBE_CHAT_MODEL", "YOLOSCRIBE_MODEL")``.
    """
    for var in env_vars:
        val = os.getenv(var, "").strip()
        if val:
            return val
    return DEFAULT_MODEL_KEY


def build_strands_model(model_key: str):
    """Return an OpenAI-compatible Strands model pointed at the LiteLLM proxy.

    The model key passes straight through as the OpenAI ``model``. Requires
    ``LITELLM_BASE_URL``; raises if unset (there is no native per-provider
    fallback — LiteLLM is the single model path).
    """
    base_url = os.getenv("LITELLM_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError(
            "LITELLM_BASE_URL is not set — all model calls route through the "
            "LiteLLM proxy. Point it at the proxy's OpenAI endpoint "
            "(e.g. http://litellm:4000/v1). See infra/litellm/config.example.yaml."
        )
    from openai import AsyncOpenAI
    from strands.models.openai import OpenAIModel

    api_key = os.getenv("LITELLM_API_KEY", "").strip() or "sk-litellm-local"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return OpenAIModel(client=client, model_id=model_key or DEFAULT_MODEL_KEY)
