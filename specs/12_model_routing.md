## Model Routing

This document describes how YoloScribe should route each agent to an appropriate Claude model, supporting both Anthropic direct and Amazon Bedrock providers.

---

### Background

All agents currently use a single hardcoded model (`claude-opus-4-6` via `AnthropicModel`). As the system matures, different agents have different cost/capability trade-offs:

| Agent | Task | Ideal model |
|---|---|---|
| `ContentWriterAgent` | Rewrites/edits markdown files | Fast, cheap — Haiku 4.5 or Sonnet 4.6 |
| `ChatAgent` (orchestrator) | Routes user intent, calls sub-agents | Smart — Sonnet 4.6 |
| `CreatorAgent` | Writes `agent.md` / `skill.md` definitions | Smart — Sonnet 4.6 |
| `SkillCreatorAgent` (future) | Writes MCP config + skill docs | Smart — Sonnet 4.6 |
| `RunnerAgent` (async SQS) | Executes arbitrary user-defined agents | Configurable per agent definition |

Extended thinking models (e.g. `claude-sonnet-4-6` with `thinking` budget) are worth routing for agents that perform planning, tool orchestration, or multi-step reasoning, but are too expensive for simple content writes.

---

### Requirements

1. **System sub-agents** (`ContentWriterAgent`, `ChatAgent`, `CreatorAgent`) must be individually configurable via environment variables, defaulting to sensible models.
2. **User-defined agents** (those run by `RunnerAgent`) must be able to specify their own model in `agent.md` under a `## Model` section.
3. **Both Anthropic and Bedrock providers** must be supported. Provider is inferred from the model key.
4. **Fallback**: any unrecognised model key falls back to the system default.
5. **No UI required** for this phase; model selection is env-var and config-file driven.

---

### Phase 1 — Model Registry (`backend/agents/models.py`)

Create a new module with a `ModelSpec` dataclass and a `MODEL_REGISTRY` dict:

```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class ModelSpec:
    provider: Literal["anthropic", "bedrock"]
    model_id: str          # exact ID sent to the API
    supports_thinking: bool = False

MODEL_REGISTRY: dict[str, ModelSpec] = {
    # Anthropic direct
    "haiku":   ModelSpec("anthropic", "claude-haiku-4-5-20251001"),
    "sonnet":  ModelSpec("anthropic", "claude-sonnet-4-6"),
    "opus":    ModelSpec("anthropic", "claude-opus-4-6"),
    # Bedrock cross-region inference profiles
    "bedrock-haiku":  ModelSpec("bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "bedrock-sonnet": ModelSpec("bedrock", "us.anthropic.claude-sonnet-4-6-20250514-v1:0"),
    "bedrock-opus":   ModelSpec("bedrock", "us.anthropic.claude-opus-4-6-20250514-v1:0"),
}

DEFAULT_MODEL_KEY = "sonnet"

def build_strands_model(model_key: str):
    """Return a strands-compatible model object for the given registry key."""
    spec = MODEL_REGISTRY.get(model_key) or MODEL_REGISTRY[DEFAULT_MODEL_KEY]
    if spec.provider == "anthropic":
        from strands.models.anthropic import AnthropicModel
        return AnthropicModel(model_id=spec.model_id)
    else:
        from strands.models.bedrock import BedrockModel
        return BedrockModel(model_id=spec.model_id)
```

`build_strands_model` is the sole factory used by all agents — no agent imports `AnthropicModel` directly.

---

### Phase 2 — Agent Definition Schema

#### `agent.md` format extension

Add an optional `## Model` section:

```markdown
# Agent: my-agent

## Description

Does useful things.

## Model

bedrock-sonnet

## Skills

- github
```

Rules:
- The value is a key from `MODEL_REGISTRY` (e.g. `sonnet`, `bedrock-opus`).
- If the section is absent or the key is unrecognised, `RunnerAgent` falls back to the `YOLOSCRIBE_RUNNER_MODEL` env var, then to `DEFAULT_MODEL_KEY`.

#### `AgentDefinition` dataclass (`backend/agents/base.py` or a shared types module)

```python
@dataclass
class AgentDefinition:
    name: str
    description: str
    skills: list[str]
    model: str = ""          # empty string = use default
```

#### `parse_agent_md()` update

```python
def parse_agent_md(text: str) -> AgentDefinition:
    # existing name/description/skills parsing …
    model = ""
    if m := re.search(r"^## Model\s*\n+(.+)", text, re.MULTILINE):
        model = m.group(1).strip()
    return AgentDefinition(name=name, description=description, skills=skills, model=model)
```

#### `put_agent()` tool update

`CreatorAgent`'s `put_agent` S3Tools method writes the `## Model` section when `model` is non-empty:

```python
def _render_agent_md(defn: AgentDefinition) -> str:
    lines = [f"# Agent: {defn.name}", "", "## Description", "", defn.description, ""]
    if defn.model:
        lines += ["## Model", "", defn.model, ""]
    lines += ["## Skills", ""]
    for skill in defn.skills:
        lines.append(f"- {skill}")
    return "\n".join(lines) + "\n"
```

---

### Phase 3 — `BaseAgent` Wiring (`backend/agents/base.py`)

Replace the hardcoded `AnthropicModel` instantiation with `build_strands_model`:

```python
# before
from strands.models.anthropic import AnthropicModel
model = AnthropicModel(model_id=os.getenv("YOLOSCRIBE_MODEL", DEFAULT_MODEL))

# after
from backend.agents.models import build_strands_model
model_key = os.getenv("YOLOSCRIBE_MODEL", DEFAULT_MODEL_KEY)
model = build_strands_model(model_key)
```

Each sub-agent class can pass its own `model_key` through an optional `__init__` parameter, overriding the env var default at construction time.

---

### Phase 4 — System Sub-Agent Env Vars

Individual env vars override the global `YOLOSCRIBE_MODEL` default for each system sub-agent:

| Env var | Used by | Default |
|---|---|---|
| `YOLOSCRIBE_MODEL` | Global fallback for all agents | `sonnet` |
| `YOLOSCRIBE_CHAT_MODEL` | `ChatAgent` (orchestrator) | `sonnet` |
| `YOLOSCRIBE_WRITER_MODEL` | `ContentWriterAgent` | `haiku` |
| `YOLOSCRIBE_CREATOR_MODEL` | `CreatorAgent` | `sonnet` |
| `YOLOSCRIBE_RUNNER_MODEL` | `RunnerAgent` default (when agent.md has no `## Model`) | `sonnet` |

Resolution order for any given agent:
1. Agent-specific env var (e.g. `YOLOSCRIBE_WRITER_MODEL`)
2. `YOLOSCRIBE_MODEL` global override
3. `DEFAULT_MODEL_KEY` (`sonnet`)

---

### Phase 5 — Agent Runner Routing (`agent-runner/`)

The async SQS worker already parses `agent.md`. After this change it must also:

1. Import a minimal copy of `MODEL_REGISTRY` and `build_strands_model` (or share via a common package).
2. Read `agent_def.model` from the parsed definition.
3. Pass that key to `build_strands_model`; fall back to `YOLOSCRIBE_RUNNER_MODEL` → `DEFAULT_MODEL_KEY`.

```python
model_key = agent_def.model or os.getenv("YOLOSCRIBE_RUNNER_MODEL", DEFAULT_MODEL_KEY)
model = build_strands_model(model_key)
agent = strands.Agent(model=model, tools=tools, system_prompt=system_prompt)
```

---

### Phase 6 — Defaults & Fallback Behaviour

| Scenario | Model used |
|---|---|
| `agent.md` has `## Model: bedrock-opus` | `bedrock-opus` |
| `agent.md` has `## Model: unknown-key` | `YOLOSCRIBE_RUNNER_MODEL` → `sonnet` |
| `agent.md` has no `## Model` section | `YOLOSCRIBE_RUNNER_MODEL` → `sonnet` |
| `ContentWriterAgent`, no env var set | `haiku` |
| `ChatAgent`, no env var set | `sonnet` |
| `YOLOSCRIBE_MODEL=opus` set globally | All agents fall back to `opus` unless overridden individually |
| No env vars set | All agents use `sonnet` |

---

### Files Changed

| File | Change |
|---|---|
| `backend/agents/models.py` | **New** — `ModelSpec`, `MODEL_REGISTRY`, `build_strands_model` |
| `backend/agents/base.py` | Replace `AnthropicModel` hardcode; add `model_key` param to `BaseAgent.__init__` |
| `backend/agents/chat.py` | Pass `model_key=os.getenv("YOLOSCRIBE_CHAT_MODEL", ...)` |
| `backend/agents/writer.py` | Pass `model_key=os.getenv("YOLOSCRIBE_WRITER_MODEL", "haiku")` |
| `backend/agents/creator.py` | Pass `model_key=os.getenv("YOLOSCRIBE_CREATOR_MODEL", ...)` |
| `backend/agents/base.py` | Update `AgentDefinition`, `parse_agent_md`, `put_agent` / `_render_agent_md` |
| `agent-runner/agent_runner/agent_runner.py` | Import registry; route by `agent_def.model` |
| `env.example` | Document new env vars |
| `CLAUDE.md` | Document new env vars |

---

### Deferred

- **Extended thinking**: `supports_thinking` flag in `ModelSpec` is reserved; enabling it (passing a `thinking` budget to the API) is deferred until Strands Agents exposes a clean API for it.
- **UI model picker**: Allowing users to pick a model in the agent editor UI is deferred — this is a low-priority quality-of-life feature.
- **Cost tracking**: Logging token counts per model per agent for cost attribution is deferred.
- **Bedrock provider config**: Bedrock requires an AWS region; this will be read from `AWS_REGION` (already present in the environment for EKS workloads).
