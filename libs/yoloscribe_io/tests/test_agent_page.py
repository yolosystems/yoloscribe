import pytest

from yoloscribe_io.events import EventType
from yoloscribe_io.storage import LocalStorageBackend
from yoloscribe_io.agent_page import (
    AgentDefinition,
    AgentDefinitionError,
    AgentMarkdownFile,
    Scope,
    build_agent_md,
    parse_agent_md,
)


# ── helpers ───────────────────────────────────────────────────────────────────

class CapturingHandler:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


def _new_format(
    trigger="manual",
    name="my-agent",
    skills=None,
    model="",
    schedule="",
    timezone="",
    confirm=False,
    ref="",
    scope_include=None,
    scope_exclude=None,
    events=None,
    body="Does something useful.",
) -> str:
    lines = ["---", f"trigger: {trigger}"]
    if name:
        lines.append(f"name: {name}")
    if ref:
        lines.append(f"ref: {ref}")
    if schedule:
        lines.append(f"schedule: {schedule}")
    if timezone:
        lines.append(f"timezone: {timezone}")
    if skills:
        lines.append("skills:")
        for s in skills:
            lines.append(f"  - {s}")
    if model:
        lines.append(f"model: {model}")
    if confirm:
        lines.append("confirm_before_write: true")
    if events:
        lines.append("events:")
        for e in events:
            lines.append(f"  - {e}")
    if scope_include or scope_exclude:
        lines.append("scope:")
        if scope_include:
            lines.append("  include:")
            for p in scope_include:
                lines.append(f"    - {p}")
        if scope_exclude:
            lines.append("  exclude:")
            for p in scope_exclude:
                lines.append(f"    - {p}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines) + "\n"


_LEGACY = """\
---
trigger: on_write
---

# Agent: legacy-agent

## Description
Does old stuff.

## Skills
- linear
- github

## Model
sonnet
"""


# ── Scope ─────────────────────────────────────────────────────────────────────

def test_scope_empty_matches_all():
    s = Scope()
    assert s.matches("projects/anything") is True
    assert s.matches("") is True


def test_scope_include_filters():
    s = Scope(include=["projects/**", "docs/**"])
    assert s.matches("projects/yoloscribe") is True
    assert s.matches("projects/yoloscribe/features") is True
    assert s.matches("notes/private") is False


def test_scope_exclude_overrides_include():
    s = Scope(include=["projects/**"], exclude=["projects/archive/**"])
    assert s.matches("projects/active") is True
    assert s.matches("projects/archive/old") is False


def test_scope_exclude_without_include():
    s = Scope(exclude=["private/**"])
    assert s.matches("public/page") is True
    assert s.matches("private/secret") is False


def test_scope_to_dict_omits_empty_lists():
    assert Scope().to_dict() == {}
    assert Scope(include=["a/**"]).to_dict() == {"include": ["a/**"]}


def test_scope_from_dict_roundtrip():
    s = Scope(include=["a/**", "b/**"], exclude=["a/skip/**"])
    restored = Scope.from_dict(s.to_dict())
    assert restored.include == ["a/**", "b/**"]
    assert restored.exclude == ["a/skip/**"]


def test_scope_from_dict_empty():
    s = Scope.from_dict({})
    assert s.include == []
    assert s.exclude == []


# ── parse_agent_md — new format ───────────────────────────────────────────────

def test_parse_minimal_new_format():
    d = parse_agent_md(_new_format())
    assert d.name == "my-agent"
    assert d.trigger == "manual"
    assert d.description == "Does something useful."


def test_parse_trigger_on_write():
    d = parse_agent_md(_new_format(trigger="on_write"))
    assert d.trigger == "on_write"


def test_parse_trigger_on_notify():
    d = parse_agent_md(_new_format(trigger="on_notify", events=["page_shared"]))
    assert d.trigger == "on_notify"


def test_parse_skills_list():
    d = parse_agent_md(_new_format(skills=["linear", "github"]))
    assert d.skills == ["linear", "github"]


def test_parse_model_field():
    d = parse_agent_md(_new_format(model="sonnet"))
    assert d.model == "sonnet"


def test_parse_confirm_before_write():
    d = parse_agent_md(_new_format(confirm=True))
    assert d.confirm_before_write is True


def test_parse_confirm_false_by_default():
    d = parse_agent_md(_new_format())
    assert d.confirm_before_write is False


def test_parse_schedule_trigger():
    d = parse_agent_md(_new_format(trigger="schedule", schedule="0 9 * * *"))
    assert d.trigger == "schedule"
    assert d.schedule == "0 9 * * *"


def test_parse_timezone():
    d = parse_agent_md(_new_format(trigger="schedule", schedule="0 9 * * *", timezone="America/New_York"))
    assert d.timezone == "America/New_York"


def test_parse_scope_include_exclude():
    d = parse_agent_md(_new_format(scope_include=["projects/**"], scope_exclude=["projects/archive/**"]))
    assert d.scope.include == ["projects/**"]
    assert d.scope.exclude == ["projects/archive/**"]


def test_parse_scope_defaults_to_empty():
    d = parse_agent_md(_new_format())
    assert d.scope.include == []
    assert d.scope.exclude == []


def test_parse_ref_field():
    d = parse_agent_md(_new_format(ref="projects/feature-backlog/.agents/sync/agent.md"))
    assert d.ref == "projects/feature-backlog/.agents/sync/agent.md"


def test_parse_ref_returns_early_with_minimal_fields():
    text = "---\ntrigger: on_write\nname: ptr\nref: other/.agents/sync/agent.md\n---\n"
    d = parse_agent_md(text)
    assert d.ref == "other/.agents/sync/agent.md"
    assert d.skills == []


# ── parse_agent_md — legacy format ───────────────────────────────────────────

def test_parse_legacy_name_from_heading():
    d = parse_agent_md(_LEGACY)
    assert d.name == "legacy-agent"


def test_parse_legacy_description_from_section():
    d = parse_agent_md(_LEGACY)
    assert d.description == "Does old stuff."


def test_parse_legacy_skills_from_section():
    d = parse_agent_md(_LEGACY)
    assert d.skills == ["linear", "github"]


def test_parse_legacy_model_from_section():
    d = parse_agent_md(_LEGACY)
    assert d.model == "sonnet"


def test_parse_legacy_trigger_from_frontmatter():
    d = parse_agent_md(_LEGACY)
    assert d.trigger == "on_write"


# ── parse_agent_md — error cases ─────────────────────────────────────────────

def test_parse_raises_no_frontmatter():
    with pytest.raises(AgentDefinitionError, match="frontmatter"):
        parse_agent_md("# Just a heading\nNo frontmatter here.")


def test_parse_raises_unclosed_frontmatter():
    with pytest.raises(AgentDefinitionError, match="closed"):
        parse_agent_md("---\ntrigger: manual\nno closing marker")


def test_parse_raises_invalid_trigger():
    with pytest.raises(AgentDefinitionError, match="trigger"):
        parse_agent_md("---\ntrigger: bad_value\nname: x\n---\n")


def test_parse_raises_schedule_without_cron():
    with pytest.raises(AgentDefinitionError, match="schedule"):
        parse_agent_md("---\ntrigger: schedule\nname: x\n---\n")


def test_parse_raises_missing_name():
    with pytest.raises(AgentDefinitionError, match="name"):
        parse_agent_md("---\ntrigger: manual\n---\nNo name here.")


# ── build_agent_md ────────────────────────────────────────────────────────────

def test_build_roundtrip_minimal():
    defn = AgentDefinition(name="sync", trigger="on_write")
    parsed = parse_agent_md(build_agent_md(defn))
    assert parsed.name == "sync"
    assert parsed.trigger == "on_write"


def test_build_roundtrip_full():
    defn = AgentDefinition(
        name="nightly",
        description="Runs every night.",
        skills=["linear", "github"],
        trigger="schedule",
        schedule="0 2 * * *",
        timezone="UTC",
        model="sonnet",
        confirm_before_write=True,
        scope=Scope(include=["projects/**"], exclude=["projects/archive/**"]),
    )
    parsed = parse_agent_md(build_agent_md(defn))
    assert parsed.name == "nightly"
    assert parsed.skills == ["linear", "github"]
    assert parsed.schedule == "0 2 * * *"
    assert parsed.timezone == "UTC"
    assert parsed.model == "sonnet"
    assert parsed.confirm_before_write is True
    assert parsed.scope.include == ["projects/**"]
    assert parsed.scope.exclude == ["projects/archive/**"]


def test_build_omits_empty_optional_fields():
    text = build_agent_md(AgentDefinition(name="x", trigger="manual"))
    assert "schedule:" not in text
    assert "timezone:" not in text
    assert "model:" not in text
    assert "confirm_before_write" not in text
    assert "scope:" not in text


def test_build_ref_field():
    defn = AgentDefinition(name="ptr", trigger="on_write", ref="other/path/.agents/a/agent.md")
    text = build_agent_md(defn)
    assert "ref: other/path/.agents/a/agent.md" in text


# ── AgentMarkdownFile ─────────────────────────────────────────────────────────

@pytest.fixture
def store():
    return LocalStorageBackend()


def test_agent_file_path_root_page(store):
    f = AgentMarkdownFile("s", "", "my-agent", store)
    assert f.path == ".agents/my-agent/agent.md"
    assert f.key == "s/.agents/my-agent/agent.md"


def test_agent_file_path_child_page(store):
    f = AgentMarkdownFile("s", "projects/yoloscribe", "sync", store)
    assert f.path == "projects/yoloscribe/.agents/sync/agent.md"
    assert f.key == "s/projects/yoloscribe/.agents/sync/agent.md"


def test_agent_file_page_path_and_name_properties(store):
    f = AgentMarkdownFile("s", "blog", "commenter", store)
    assert f.page_path == "blog"
    assert f.agent_name == "commenter"


def test_agent_file_definition_property(store):
    defn = AgentDefinition(name="syncer", trigger="on_write", skills=["linear"])
    f = AgentMarkdownFile("s", "p", "syncer", store, content=build_agent_md(defn))
    parsed = f.definition
    assert parsed.name == "syncer"
    assert parsed.trigger == "on_write"
    assert parsed.skills == ["linear"]


def test_agent_file_create_writes_content(store):
    defn = AgentDefinition(name="a", trigger="manual")
    f = AgentMarkdownFile("s", "p", "a", store)
    f.create(defn)
    assert store.read("s/p/.agents/a/agent.md") is not None


def test_agent_file_create_emits_agent_created(store):
    defn = AgentDefinition(name="a", trigger="manual")
    f = AgentMarkdownFile("s", "p", "a", store)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.create(defn)
    assert cap.events[0].type == EventType.AGENT_CREATED
    assert cap.events[0].payload["agent_name"] == "a"
    assert cap.events[0].payload["page_path"] == "p"


def test_agent_file_save_updates_content(store):
    defn_v1 = AgentDefinition(name="a", trigger="manual")
    defn_v2 = AgentDefinition(name="a", trigger="on_write")
    f = AgentMarkdownFile("s", "p", "a", store)
    f.create(defn_v1)
    f.save(defn_v2)
    assert f.definition.trigger == "on_write"


def test_agent_file_save_emits_agent_updated(store):
    defn = AgentDefinition(name="a", trigger="manual")
    f = AgentMarkdownFile("s", "p", "a", store)
    f.create(defn)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.save(AgentDefinition(name="a", trigger="on_write"))
    assert cap.events[0].type == EventType.AGENT_UPDATED


def test_agent_file_delete_removes_from_storage(store):
    defn = AgentDefinition(name="a", trigger="manual")
    f = AgentMarkdownFile("s", "p", "a", store)
    f.create(defn)
    f.delete()
    assert store.read("s/p/.agents/a/agent.md") is None


def test_agent_file_delete_emits_agent_deleted(store):
    defn = AgentDefinition(name="a", trigger="manual")
    f = AgentMarkdownFile("s", "p", "a", store)
    f.create(defn)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.delete()
    assert cap.events[0].type == EventType.AGENT_DELETED


def test_agent_file_create_content_parseable(store):
    defn = AgentDefinition(
        name="nightly",
        description="Runs every night.",
        skills=["linear"],
        trigger="schedule",
        schedule="0 2 * * *",
    )
    f = AgentMarkdownFile("s", "p", "nightly", store)
    f.create(defn)
    parsed = f.definition
    assert parsed.name == "nightly"
    assert parsed.description == "Runs every night."
    assert parsed.schedule == "0 2 * * *"


# ── events field ──────────────────────────────────────────────────────────────

def test_on_notify_without_events_raises():
    text = "---\ntrigger: on_notify\nname: n\n---\n"
    with pytest.raises(AgentDefinitionError, match="events"):
        parse_agent_md(text)


def test_on_notify_with_empty_events_list_raises():
    text = "---\ntrigger: on_notify\nname: n\nevents: []\n---\n"
    with pytest.raises(AgentDefinitionError, match="events"):
        parse_agent_md(text)


def test_on_notify_with_events_parses():
    text = "---\ntrigger: on_notify\nname: n\nevents:\n  - page_shared\n---\n"
    defn = parse_agent_md(text)
    assert defn.events == ["page_shared"]
    assert defn.trigger == "on_notify"


def test_on_notify_with_multiple_events():
    text = "---\ntrigger: on_notify\nname: n\nevents:\n  - page_shared\n  - access_requested\n---\n"
    defn = parse_agent_md(text)
    assert defn.events == ["page_shared", "access_requested"]


def test_manual_trigger_events_not_required():
    text = "---\ntrigger: manual\nname: n\n---\n"
    defn = parse_agent_md(text)
    assert defn.events == []


def test_on_write_trigger_events_not_required():
    text = "---\ntrigger: on_write\nname: n\n---\n"
    defn = parse_agent_md(text)
    assert defn.events == []


def test_build_agent_md_includes_events():
    defn = AgentDefinition(
        name="n", trigger="on_notify", events=["page_shared", "access_requested"]
    )
    text = build_agent_md(defn)
    assert "events:" in text
    assert "  - page_shared" in text
    assert "  - access_requested" in text


def test_build_agent_md_omits_events_when_empty():
    defn = AgentDefinition(name="n", trigger="manual")
    text = build_agent_md(defn)
    assert "events:" not in text


def test_events_roundtrip():
    defn = AgentDefinition(
        name="n", trigger="on_notify", events=["page_shared", "confirm_page_change"]
    )
    parsed = parse_agent_md(build_agent_md(defn))
    assert parsed.events == ["page_shared", "confirm_page_change"]


def test_ref_agent_events_optional():
    text = "---\ntrigger: on_notify\nname: ptr\nref: .agents/target/agent.md\n---\n"
    defn = parse_agent_md(text)
    assert defn.ref == ".agents/target/agent.md"
    assert defn.events == []


def test_ref_agent_events_preserved_when_set():
    text = (
        "---\ntrigger: on_notify\nname: ptr\n"
        "ref: .agents/target/agent.md\n"
        "events:\n  - page_shared\n---\n"
    )
    defn = parse_agent_md(text)
    assert defn.events == ["page_shared"]


# ── type field ────────────────────────────────────────────────────────────────

def test_parse_type_page():
    text = "---\ntrigger: on_write\nname: n\ntype: page\n---\n"
    defn = parse_agent_md(text)
    assert defn.type == "page"


def test_parse_type_ingest():
    text = "---\ntrigger: on_write\nname: n\ntype: ingest\n---\n"
    defn = parse_agent_md(text)
    assert defn.type == "ingest"


def test_parse_type_notification():
    text = "---\ntrigger: on_notify\nname: n\ntype: notification\nevents:\n  - page_shared\n---\n"
    defn = parse_agent_md(text)
    assert defn.type == "notification"


def test_parse_type_absent_defaults_to_page():
    text = "---\ntrigger: on_write\nname: n\n---\n"
    defn = parse_agent_md(text)
    assert defn.type == "page"


def test_parse_type_invalid_raises():
    text = "---\ntrigger: on_write\nname: n\ntype: robot\n---\n"
    with pytest.raises(AgentDefinitionError, match="type"):
        parse_agent_md(text)


def test_build_type_field_included():
    defn = AgentDefinition(name="n", trigger="on_write", type="ingest")
    text = build_agent_md(defn)
    assert "type: ingest" in text


def test_build_type_omitted_when_empty():
    defn = AgentDefinition(name="n", trigger="on_write")
    text = build_agent_md(defn)
    assert "type:" not in text


def test_type_roundtrip():
    for agent_type in ("page", "ingest", "notification"):
        events = ["page_shared"] if agent_type == "notification" else []
        trigger = "on_notify" if agent_type == "notification" else "on_write"
        defn = AgentDefinition(name="n", trigger=trigger, type=agent_type, events=events)
        parsed = parse_agent_md(build_agent_md(defn))
        assert parsed.type == agent_type
