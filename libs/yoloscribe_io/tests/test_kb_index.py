from yoloscribe_io.storage import LocalStorageBackend
from yoloscribe_io.kb_index import KnowledgeBaseIndexMarkdownFile


def _store(content: str) -> LocalStorageBackend:
    return LocalStorageBackend({"s/.user/ingest/content.md": content})


# ── construction ──────────────────────────────────────────────────────────────

def test_key():
    f = KnowledgeBaseIndexMarkdownFile("mysite", LocalStorageBackend())
    assert f.key == "mysite/.user/ingest/content.md"


def test_path():
    f = KnowledgeBaseIndexMarkdownFile("mysite", LocalStorageBackend())
    assert f.path == ".user/ingest/content.md"


# ── topics property ───────────────────────────────────────────────────────────

def test_topics_empty_file():
    f = KnowledgeBaseIndexMarkdownFile("s", LocalStorageBackend())
    assert f.topics == []


def test_topics_parses_dash_list():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("- jazz\n- cooking\n"))
    assert f.topics == ["jazz", "cooking"]


def test_topics_parses_asterisk_list():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("* jazz\n* cooking\n"))
    assert f.topics == ["jazz", "cooking"]


def test_topics_parses_plus_list():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("+ jazz\n+ cooking\n"))
    assert f.topics == ["jazz", "cooking"]


def test_topics_mixed_bullet_styles():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("- jazz\n* cooking\n+ tech\n"))
    assert f.topics == ["jazz", "cooking", "tech"]


def test_topics_skips_blank_lines():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("- jazz\n\n- cooking\n"))
    assert f.topics == ["jazz", "cooking"]


def test_topics_skips_non_list_lines():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("# Heading\n- jazz\nsome prose\n- cooking\n"))
    assert f.topics == ["jazz", "cooking"]


def test_topics_strips_whitespace():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("-  jazz  \n"))
    assert f.topics == ["jazz"]


def test_topics_skips_empty_bullet():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("- \n- cooking\n"))
    assert f.topics == ["cooking"]


# ── update_topics ─────────────────────────────────────────────────────────────

def test_update_topics_writes_list():
    store = LocalStorageBackend()
    f = KnowledgeBaseIndexMarkdownFile("s", store)
    f.update_topics(["jazz", "cooking"])
    assert store.read("s/.user/ingest/content.md") == "- jazz\n- cooking\n"


def test_update_topics_empty_list_writes_empty_string():
    store = LocalStorageBackend()
    f = KnowledgeBaseIndexMarkdownFile("s", store)
    f.update_topics([])
    assert store.read("s/.user/ingest/content.md") == ""


def test_update_topics_replaces_existing_content():
    store = LocalStorageBackend({"s/.user/ingest/content.md": "- old\n"})
    f = KnowledgeBaseIndexMarkdownFile("s", store)
    f.update_topics(["new"])
    assert store.read("s/.user/ingest/content.md") == "- new\n"


def test_topics_reflects_after_update():
    store = LocalStorageBackend()
    f = KnowledgeBaseIndexMarkdownFile("s", store)
    f.update_topics(["jazz", "cooking"])
    assert f.topics == ["jazz", "cooking"]


def test_single_topic():
    f = KnowledgeBaseIndexMarkdownFile("s", _store("- jazz\n"))
    assert f.topics == ["jazz"]
