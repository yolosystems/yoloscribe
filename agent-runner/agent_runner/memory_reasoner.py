"""MemoryReasoner — derives explicit/deductive conclusions from preference signals."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# Anthropic model IDs for the reasoners (Bedrock/GLM keys are not supported
# since the reasoners call anthropic.Anthropic directly).
_ANTHROPIC_MODEL_REGISTRY: dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
}


def _resolve_anthropic_model(env_var: str, default_key: str) -> str:
    """Resolve an Anthropic model ID from a single env var, falling back to default_key.

    bedrock-* and glm keys are not valid — this client uses anthropic.Anthropic directly.
    """
    val = os.environ.get(env_var, default_key).strip() or default_key
    model_id = _ANTHROPIC_MODEL_REGISTRY.get(val)
    if model_id:
        return model_id
    log.warning(
        "MemoryReasoner: %s=%r is not a recognised Anthropic model key "
        "(valid: %s) — using default '%s'",
        env_var, val, ", ".join(_ANTHROPIC_MODEL_REGISTRY), default_key,
    )
    return _ANTHROPIC_MODEL_REGISTRY[default_key]


_SYSTEM_PROMPT = """\
You are a precision memory reasoner for YoloScribe, an AI-powered wiki.

Your role: derive atomic, certain conclusions from a preference signal.

RULES:
1. Emit ONLY 'explicit' (directly stated by the user) or 'deductive' (necessarily follows) conclusions.
2. Never produce 'inductive' or 'abductive' tiers — those belong to a later scheduled pass.
3. Each conclusion statement must be ≤ 500 characters and fully self-contained.
4. Evidence must reference only user actions, never the content of wiki pages.
5. Return valid YAML only — a list. If no certain conclusions can be derived, return [].
6. Use unique IDs of the form c-{6 random lowercase hex chars}, e.g. c-3a9f12.

SCAFFOLDING RULE:
- Explicit conclusions: derived_from must be empty [].
- Deductive conclusions: derived_from may only list IDs of explicit conclusions already present.

DOMAINS: ingest | enrich | retrieve | notify | present

Signal types and what they mean:
- agent_run_success: an agent completed successfully on a wiki page
- agent_run_failure: an agent failed on a wiki page
- proposal_accepted: user accepted a staged agent or content proposal
- proposal_rejected: user rejected a staged proposal
- user_instruction: user explicitly stated a preference in conversation
- agent_created: user created a new agent definition (via MCP or chat)
- agent_deleted: user deleted an agent (via MCP or chat)
- page_created: user created a new wiki page via the MCP tool suite
- page_updated: user updated a wiki page directly via the MCP tool suite
- notification_ignored: a notification was not acted on

Required YAML schema for each conclusion in your response:
  - id: c-xxxxxx
    level: explicit | deductive
    domain: ingest | enrich | retrieve | notify | present
    statement: <≤500-char atomic statement>
    evidence:
      - {type: <signal_type>, at: <iso_datetime>}
    derived_from: []
    status: active"""


def _user_message(signal_dict: dict[str, Any], existing_yaml: str) -> str:
    import yaml

    signal_yaml = yaml.dump(signal_dict, default_flow_style=False, allow_unicode=True)
    parts = [f"Signal:\n{signal_yaml}"]
    if existing_yaml.strip():
        parts.append(
            f"Existing conclusions (premises available for deductive reasoning):\n{existing_yaml}"
        )
    parts.append(
        "Return a YAML list of new conclusions, or [] if none can be derived with certainty."
    )
    return "\n\n".join(parts)


# ── Interface ─────────────────────────────────────────────────────────────────

class MemoryReasoner:
    """Abstract interface for deriving memory conclusions from preference signals."""

    def derive(
        self,
        signal_type: str,
        payload: dict[str, Any],
        existing_yaml: str = "",
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class NullMemoryReasoner(MemoryReasoner):
    """No-op reasoner — used when memory is disabled or in tests."""

    def derive(self, signal_type, payload, existing_yaml=""):
        return []


class HaikuMemoryReasoner(MemoryReasoner):
    """Derives explicit/deductive conclusions per preference signal.

    Model defaults to haiku; override with YOLOSCRIBE_MEMORY_REASONER_MODEL.
    Only Anthropic-provider keys are accepted (haiku|sonnet|opus).
    """

    _DEFAULT_KEY = "haiku"
    _MAX_TOKENS = 1024

    def derive(
        self,
        signal_type: str,
        payload: dict[str, Any],
        existing_yaml: str = "",
    ) -> list[dict[str, Any]]:
        import anthropic
        import yaml

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return []

        model_id = _resolve_anthropic_model(
            "YOLOSCRIBE_MEMORY_REASONER_MODEL",
            default_key=self._DEFAULT_KEY,
        )
        client_kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 1}
        base_url = os.environ.get("YOLOSCRIBE_MODEL_BASE_URL", "").strip()
        if base_url:
            client_kwargs["base_url"] = base_url

        client = anthropic.Anthropic(**client_kwargs)
        signal_dict = {"type": signal_type, **payload}
        user_msg = _user_message(signal_dict, existing_yaml)

        try:
            response = client.messages.create(
                model=model_id,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:
            log.warning("MemoryReasoner Haiku call failed: %s", exc)
            return []

        text = response.content[0].text.strip() if response.content else ""
        if not text or text == "[]":
            return []

        # Strip markdown code fences if the model wrapped the YAML.
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())

        try:
            items = yaml.safe_load(text)
            if not isinstance(items, list):
                log.warning("MemoryReasoner returned non-list: %r", text[:200])
                return []
            return [item for item in items if isinstance(item, dict)]
        except Exception as exc:
            log.warning("MemoryReasoner YAML parse failed: %s — %r", exc, text[:200])
            return []


_CONSOLIDATION_SYSTEM_PROMPT = """\
You are a consolidation memory reasoner for YoloScribe, an AI-powered wiki.

Your role: perform an inductive and abductive reasoning pass over a full preference signal log,
derive durable pattern-level conclusions, and identify stale conclusions to decay.

RULES:
1. Emit ONLY 'inductive' (pattern from repeated signals) or 'abductive' (best explanation for
   observed behaviour) conclusions. Never emit 'explicit' or 'deductive' — those belong to
   the per-signal pass.
2. Each conclusion statement must be ≤ 500 characters and fully self-contained.
3. Return valid YAML with two top-level keys: 'conclusions' (list) and 'decay' (list of IDs).
4. Use unique IDs of the form c-{6 random lowercase hex chars}, e.g. c-3a9f12.
5. Only emit a conclusion when there is clear pattern evidence across multiple signals.
6. In the 'decay' list, include IDs of existing conclusions that are contradicted by recent
   signals or that have not been reinforced in a long time and should be reconsidered.
7. If no conclusions can be derived and nothing should decay, return:
   conclusions: []
   decay: []

DOMAINS: ingest | enrich | retrieve | notify | present

Required YAML schema for each conclusion in 'conclusions':
  - id: c-xxxxxx
    level: inductive | abductive
    domain: ingest | enrich | retrieve | notify | present
    statement: <≤500-char atomic statement>
    evidence:
      - {type: <signal_type>, at: <iso_datetime_or_approximate>}
    derived_from: []
    status: active"""


def _consolidation_user_message(signal_log_text: str, existing_yaml: str) -> str:
    parts = [f"Full signal log (newest first):\n{signal_log_text}"]
    if existing_yaml.strip():
        parts.append(
            f"Existing conclusions (for context and decay identification):\n{existing_yaml}"
        )
    parts.append(
        "Return YAML with 'conclusions' (new inductive/abductive) and 'decay' (IDs to mark stale)."
    )
    return "\n\n".join(parts)


class ConsolidationMemoryReasoner:
    """Derives inductive/abductive conclusions from the full signal log.

    Model defaults to sonnet; override with YOLOSCRIBE_CONSOLIDATION_REASONER_MODEL.
    Only Anthropic-provider keys are accepted (haiku|sonnet|opus).
    """

    _DEFAULT_KEY = "sonnet"
    _MAX_TOKENS = 4096

    def consolidate(
        self,
        signal_log_text: str,
        existing_yaml: str = "",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Derive inductive/abductive conclusions and identify stale ones.

        Returns (new_conclusions, decay_ids).
        """
        import anthropic
        import yaml

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return [], []

        if not signal_log_text.strip():
            return [], []

        model_id = _resolve_anthropic_model(
            "YOLOSCRIBE_CONSOLIDATION_REASONER_MODEL",
            default_key=self._DEFAULT_KEY,
        )
        client_kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 1}
        base_url = os.environ.get("YOLOSCRIBE_MODEL_BASE_URL", "").strip()
        if base_url:
            client_kwargs["base_url"] = base_url

        client = anthropic.Anthropic(**client_kwargs)
        user_msg = _consolidation_user_message(signal_log_text, existing_yaml)

        try:
            response = client.messages.create(
                model=model_id,
                max_tokens=self._MAX_TOKENS,
                system=_CONSOLIDATION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:
            log.warning("ConsolidationMemoryReasoner Sonnet call failed: %s", exc)
            return [], []

        text = response.content[0].text.strip() if response.content else ""
        if not text:
            return [], []

        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())

        try:
            parsed = yaml.safe_load(text)
            if not isinstance(parsed, dict):
                log.warning("ConsolidationMemoryReasoner returned non-dict: %r", text[:200])
                return [], []
            conclusions = [
                item for item in (parsed.get("conclusions") or [])
                if isinstance(item, dict)
            ]
            decay = [str(x) for x in (parsed.get("decay") or []) if x]
            return conclusions, decay
        except Exception as exc:
            log.warning("ConsolidationMemoryReasoner YAML parse failed: %s — %r", exc, text[:200])
            return [], []
