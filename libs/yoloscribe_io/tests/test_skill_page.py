import pytest

from yoloscribe_io.events import EventType
from yoloscribe_io.storage import LocalStorageBackend
from yoloscribe_io.skill_page import (
    SKILL_NAME_RE,
    SkillDefinition,
    SkillMarkdownFile,
    build_skill_md,
    parse_skill_md,
)


# ── helpers ───────────────────────────────────────────────────────────────────

class CapturingHandler:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


_FULL_SKILL = """\
---
name: notifications
description: Send outbound notifications via webhook
tools:
  - put_notification
  - list_webhooks
---

You have access to put_notification and list_webhooks.

## Guidelines

- Keep messages concise.
"""

_NO_FRONTMATTER = "Just plain text, no YAML block."

_EMPTY_FRONTMATTER = "---\n---\nInstructions only."

_TOOLS_ONLY = "---\ntools:\n  - linear_create_issue\n  - linear_get_issue\n---\nDo linear stuff."


# ── SKILL_NAME_RE ─────────────────────────────────────────────────────────────

def test_skill_name_re_valid():
    for name in ("linear", "github", "my-skill", "skill_v2", "a1"):
        assert SKILL_NAME_RE.match(name), f"expected match: {name}"


def test_skill_name_re_invalid():
    for name in ("", "-bad", "Bad", "has space", "has/slash"):
        assert not SKILL_NAME_RE.match(name), f"expected no match: {name}"


# ── parse_skill_md ────────────────────────────────────────────────────────────

def test_parse_full_skill():
    d = parse_skill_md(_FULL_SKILL, name="notifications")
    assert d.name == "notifications"
    assert d.description == "Send outbound notifications via webhook"
    assert d.tools == ["put_notification", "list_webhooks"]
    assert "put_notification" in d.instructions


def test_parse_name_from_frontmatter_when_not_passed():
    d = parse_skill_md(_FULL_SKILL)
    assert d.name == "notifications"


def test_parse_name_arg_overrides_frontmatter():
    d = parse_skill_md(_FULL_SKILL, name="override")
    assert d.name == "override"


def test_parse_no_frontmatter_returns_defaults():
    d = parse_skill_md(_NO_FRONTMATTER, name="x")
    assert d.name == "x"
    assert d.description == ""
    assert d.tools == []
    assert d.instructions == _NO_FRONTMATTER


def test_parse_empty_frontmatter_returns_defaults():
    d = parse_skill_md(_EMPTY_FRONTMATTER, name="x")
    assert d.description == ""
    assert d.tools == []
    assert d.instructions == "Instructions only."


def test_parse_tools_only():
    d = parse_skill_md(_TOOLS_ONLY, name="linear")
    assert d.tools == ["linear_create_issue", "linear_get_issue"]
    assert d.instructions == "Do linear stuff."


def test_parse_missing_description_defaults_empty():
    text = "---\ntools:\n  - my_tool\n---\nBody."
    d = parse_skill_md(text, name="x")
    assert d.description == ""
    assert d.tools == ["my_tool"]


def test_parse_missing_tools_defaults_empty():
    text = "---\ndescription: A skill\n---\nBody."
    d = parse_skill_md(text, name="x")
    assert d.tools == []
    assert d.description == "A skill"


def test_parse_instructions_stripped():
    text = "---\ndescription: x\n---\n\n\n  Body here.  \n\n"
    d = parse_skill_md(text, name="x")
    assert d.instructions == "Body here."


def test_parse_never_raises_on_bad_content():
    for content in ("", "---", "---\nbad: yaml: :\n---", "---\n---"):
        d = parse_skill_md(content, name="x")
        assert isinstance(d, SkillDefinition)


def test_parse_single_tool_string():
    text = "---\ntools: my_tool\n---\n"
    d = parse_skill_md(text, name="x")
    assert d.tools == ["my_tool"]


# ── build_skill_md ────────────────────────────────────────────────────────────

def test_build_roundtrip_full():
    defn = SkillDefinition(
        name="linear",
        description="Linear project management",
        tools=["linear_create_issue", "linear_get_issue"],
        instructions="Use these tools to manage Linear issues.",
    )
    parsed = parse_skill_md(build_skill_md(defn), name="linear")
    assert parsed.description == "Linear project management"
    assert parsed.tools == ["linear_create_issue", "linear_get_issue"]
    assert parsed.instructions == "Use these tools to manage Linear issues."


def test_build_roundtrip_minimal():
    defn = SkillDefinition(name="x")
    parsed = parse_skill_md(build_skill_md(defn), name="x")
    assert parsed.description == ""
    assert parsed.tools == []
    assert parsed.instructions == ""


def test_build_omits_empty_fields():
    text = build_skill_md(SkillDefinition(name="x"))
    assert "description:" not in text
    assert "tools:" not in text


def test_build_includes_all_tools():
    defn = SkillDefinition(name="x", tools=["a", "b", "c"])
    text = build_skill_md(defn)
    assert "  - a" in text
    assert "  - b" in text
    assert "  - c" in text


def test_build_includes_instructions_in_body():
    defn = SkillDefinition(name="x", instructions="Do this first.\nThen do that.")
    text = build_skill_md(defn)
    assert "Do this first." in text
    assert "Then do that." in text


# ── SkillMarkdownFile ─────────────────────────────────────────────────────────

@pytest.fixture
def store():
    return LocalStorageBackend()


def test_skill_file_key(store):
    f = SkillMarkdownFile("mysite", "linear", store)
    assert f.key == "mysite/.skills/linear/SKILL.md"
    assert f.path == ".skills/linear/SKILL.md"


def test_skill_file_skill_name_property(store):
    f = SkillMarkdownFile("s", "github", store)
    assert f.skill_name == "github"


def test_skill_file_definition_property(store):
    defn = SkillDefinition(name="linear", description="Linear skill", tools=["linear_get_issue"])
    f = SkillMarkdownFile("s", "linear", store, content=build_skill_md(defn))
    d = f.definition
    assert d.name == "linear"
    assert d.description == "Linear skill"
    assert d.tools == ["linear_get_issue"]


def test_skill_file_create_writes_to_storage(store):
    defn = SkillDefinition(name="linear", tools=["linear_get_issue"])
    f = SkillMarkdownFile("s", "linear", store)
    f.create(defn)
    assert store.read("s/.skills/linear/SKILL.md") is not None


def test_skill_file_create_emits_skill_created(store):
    defn = SkillDefinition(name="linear", tools=["linear_get_issue"])
    f = SkillMarkdownFile("s", "linear", store)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.create(defn)
    ev = cap.events[0]
    assert ev.type == EventType.SKILL_CREATED
    assert ev.payload["skill_name"] == "linear"
    assert ev.payload["site"] == "s"
    assert ev.payload["tools"] == ["linear_get_issue"]


def test_skill_file_save_updates_storage(store):
    defn_v1 = SkillDefinition(name="linear", tools=["linear_get_issue"])
    defn_v2 = SkillDefinition(name="linear", tools=["linear_get_issue", "linear_create_issue"])
    f = SkillMarkdownFile("s", "linear", store)
    f.create(defn_v1)
    f.save(defn_v2)
    assert f.definition.tools == ["linear_get_issue", "linear_create_issue"]


def test_skill_file_save_emits_skill_changed(store):
    defn = SkillDefinition(name="linear", tools=["linear_get_issue"])
    f = SkillMarkdownFile("s", "linear", store)
    f.create(defn)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.save(SkillDefinition(name="linear", tools=["linear_get_issue", "linear_create_issue"]))
    assert cap.events[0].type == EventType.SKILL_CHANGED


def test_skill_file_save_not_skill_updated(store):
    """save() emits skill.changed, not skill.updated — breaking-change semantics."""
    defn = SkillDefinition(name="x")
    f = SkillMarkdownFile("s", "x", store)
    f.create(defn)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.save(defn)
    assert cap.events[0].type != EventType.SKILL_CREATED
    assert cap.events[0].type == EventType.SKILL_CHANGED


def test_skill_file_delete_removes_from_storage(store):
    defn = SkillDefinition(name="linear", tools=["t"])
    f = SkillMarkdownFile("s", "linear", store)
    f.create(defn)
    f.delete()
    assert store.read("s/.skills/linear/SKILL.md") is None


def test_skill_file_delete_emits_skill_deleted(store):
    defn = SkillDefinition(name="linear")
    f = SkillMarkdownFile("s", "linear", store)
    f.create(defn)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.delete()
    ev = cap.events[0]
    assert ev.type == EventType.SKILL_DELETED
    assert ev.payload["skill_name"] == "linear"
    assert ev.payload["site"] == "s"


def test_skill_file_content_at_construction(store):
    content = build_skill_md(SkillDefinition(name="n", description="D", tools=["t"]))
    f = SkillMarkdownFile("s", "n", store, content=content)
    assert f.definition.description == "D"


def test_skill_file_definition_uses_path_name(store):
    """skill_name from constructor is the canonical name, even if frontmatter differs."""
    content = build_skill_md(SkillDefinition(name="wrong-name", tools=["t"]))
    f = SkillMarkdownFile("s", "correct-name", store, content=content)
    assert f.definition.name == "correct-name"
