"""A strands `Model` test double that plays back a scripted sequence of turns.

No network calls, no cost, fully deterministic — used by the conformance
harness wherever a full `agent.run()` cycle needs to be driven without a real,
paid LLM call. This deliberately does NOT test model *judgement* — it proves
the surrounding infra (write-path, signal emission, idempotency, proposal
staging) behaves correctly when the agent calls tools in a given order. R5's
model-judgement claim is the one place a scripted model would be circular, so
that criterion uses a real model instead (see test_r5_routing_opinion.py).
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field
from typing import Any

from strands.models.model import Model
from strands.types.streaming import StreamEvent


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any] = field(default_factory=dict)


class ScriptExhausted(RuntimeError):
    """Raised when the agent asks for another turn but the script has none left."""


class ScriptedModel(Model):
    """Plays back `script`, one entry per `stream()` call (i.e. per agent turn).

    Each entry is either:
    - a str — emitted as final assistant text (stopReason="end_turn")
    - a list[ToolCall] — emitted as tool-use content blocks (stopReason="tool_use")
    """

    def __init__(self, script: list[str | list[ToolCall]]) -> None:
        self._script = list(script)
        self._index = 0
        self._config: dict[str, Any] = {}

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> Any:
        return self._config

    async def structured_output(
        self, output_model, prompt, system_prompt=None, **kwargs: Any
    ) -> AsyncGenerator[dict, None]:
        raise NotImplementedError("ScriptedModel does not support structured_output")
        yield  # pragma: no cover -- makes this an async generator function

    async def stream(
        self,
        messages,
        tool_specs=None,
        system_prompt=None,
        *,
        tool_choice=None,
        system_prompt_content=None,
        invocation_state=None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        if self._index >= len(self._script):
            raise ScriptExhausted(
                f"ScriptedModel script exhausted after {self._index} turn(s); "
                "the agent asked for another turn than the test scripted."
            )
        turn = self._script[self._index]
        self._index += 1

        yield {"messageStart": {"role": "assistant"}}

        if isinstance(turn, str):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": turn}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            for call in turn:
                tool_use_id = f"tooluse_{uuid.uuid4().hex[:24]}"
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"name": call.name, "toolUseId": tool_use_id}}
                    }
                }
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(call.input)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}

        yield {
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 0},
            }
        }
