import pytest
from yoloscribe_io.markdown_file import MarkdownFile, _parse_frontmatter
from yoloscribe_io.storage import LocalStorageBackend


# ── _parse_frontmatter ─────────────────────────────────────────────────────────

def test_parse_frontmatter_no_block():
    fm, body = _parse_frontmatter("# Hello\nworld")
    assert fm == {}
    assert body == "# Hello\nworld"


def test_parse_frontmatter_empty_block():
    fm, body = _parse_frontmatter("---\n---\n# Body")
    assert fm == {}
    assert body == "# Body"


def test_parse_frontmatter_scalar_fields():
    text = "---\ntrigger: on_write\nname: my-agent\n---\nbody text"
    fm, body = _parse_frontmatter(text)
    assert fm == {"trigger": "on_write", "name": "my-agent"}
    assert body == "body text"


def test_parse_frontmatter_list_field():
    text = "---\nskills:\n  - linear\n  - github\n---\nbody"
    fm, body = _parse_frontmatter(text)
    assert fm["skills"] == ["linear", "github"]
    assert body == "body"


def test_parse_frontmatter_boolean_field():
    text = "---\nconfirm_before_write: true\n---\n"
    fm, _ = _parse_frontmatter(text)
    assert fm["confirm_before_write"] is True


def test_parse_frontmatter_unclosed_block():
    fm, body = _parse_frontmatter("---\ntrigger: on_write\nbody without close")
    assert fm == {}


def test_parse_frontmatter_strips_leading_newlines_from_body():
    text = "---\nkey: val\n---\n\n\nbody"
    _, body = _parse_frontmatter(text)
    assert body == "body"


# ── MarkdownFile ───────────────────────────────────────────────────────────────

@pytest.fixture
def store() -> LocalStorageBackend:
    return LocalStorageBackend()


def test_key_construction(store):
    f = MarkdownFile("mysite", "page/content.md", store)
    assert f.key == "mysite/page/content.md"


def test_site_and_path_immutable(store):
    f = MarkdownFile("s", "p/content.md", store)
    assert f.site == "s"
    assert f.path == "p/content.md"


def test_read_returns_storage_content(store):
    store.write("s/p/content.md", "# Hello")
    f = MarkdownFile("s", "p/content.md", store)
    assert f.read() == "# Hello"


def test_read_empty_when_missing(store):
    f = MarkdownFile("s", "p/content.md", store)
    assert f.read() == ""


def test_write_persists_to_storage(store):
    f = MarkdownFile("s", "p/content.md", store)
    f.write("# New content")
    assert store.read("s/p/content.md") == "# New content"


def test_write_updates_raw_content_cache(store):
    f = MarkdownFile("s", "p/content.md", store)
    f.write("# Cached")
    assert f.raw_content == "# Cached"


def test_content_provided_at_construction(store):
    f = MarkdownFile("s", "p/content.md", store, content="# Preloaded")
    assert f.raw_content == "# Preloaded"


def test_raw_content_lazy_loads(store):
    store.write("s/p/content.md", "lazy")
    f = MarkdownFile("s", "p/content.md", store)
    assert f._raw_content is None
    assert f.raw_content == "lazy"
    assert f._raw_content == "lazy"


def test_frontmatter_property(store):
    content = "---\ntrigger: schedule\nname: nightly\n---\nDo stuff nightly."
    f = MarkdownFile("s", "p/content.md", store, content=content)
    assert f.frontmatter == {"trigger": "schedule", "name": "nightly"}


def test_content_property(store):
    content = "---\nkey: val\n---\nBody here."
    f = MarkdownFile("s", "p/content.md", store, content=content)
    assert f.content == "Body here."


def test_content_property_no_frontmatter(store):
    f = MarkdownFile("s", "p/content.md", store, content="Just a body.")
    assert f.content == "Just a body."
    assert f.frontmatter == {}


def test_read_refreshes_from_storage(store):
    store.write("s/p/content.md", "v1")
    f = MarkdownFile("s", "p/content.md", store, content="stale")
    assert f.read() == "v1"
    assert f.raw_content == "v1"
