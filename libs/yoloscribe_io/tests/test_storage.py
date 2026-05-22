import pytest
from yoloscribe_io.storage import LocalStorageBackend


@pytest.fixture
def store() -> LocalStorageBackend:
    return LocalStorageBackend()


@pytest.fixture
def populated() -> LocalStorageBackend:
    return LocalStorageBackend({"site/page/content.md": "# Hello"})


# ── read ───────────────────────────────────────────────────────────────────────

def test_read_missing_returns_none(store):
    assert store.read("does/not/exist") is None


def test_read_returns_written_content(store):
    store.write("a/b.md", "content")
    assert store.read("a/b.md") == "content"


def test_read_from_initial(populated):
    assert populated.read("site/page/content.md") == "# Hello"


# ── read_with_etag ─────────────────────────────────────────────────────────────

def test_read_with_etag_missing(store):
    content, etag = store.read_with_etag("missing")
    assert content is None
    assert etag is None


def test_read_with_etag_returns_content_and_etag(store):
    store.write("k", "v")
    content, etag = store.read_with_etag("k")
    assert content == "v"
    assert etag is not None


def test_etag_changes_on_write(store):
    store.write("k", "v1")
    _, etag1 = store.read_with_etag("k")
    store.write("k", "v2")
    _, etag2 = store.read_with_etag("k")
    assert etag1 != etag2


# ── write ──────────────────────────────────────────────────────────────────────

def test_write_overwrites(store):
    store.write("k", "first")
    store.write("k", "second")
    assert store.read("k") == "second"


# ── write_conditional ──────────────────────────────────────────────────────────

def test_write_conditional_no_etag_always_succeeds(store):
    assert store.write_conditional("k", "v", etag=None) is True
    assert store.read("k") == "v"


def test_write_conditional_matching_etag_succeeds(store):
    store.write("k", "original")
    _, etag = store.read_with_etag("k")
    assert store.write_conditional("k", "updated", etag=etag) is True
    assert store.read("k") == "updated"


def test_write_conditional_stale_etag_fails(store):
    store.write("k", "original")
    _, etag = store.read_with_etag("k")
    store.write("k", "interleaved")
    assert store.write_conditional("k", "lost update", etag=etag) is False
    assert store.read("k") == "interleaved"


def test_write_conditional_wrong_etag_does_not_write(store):
    store.write("k", "original")
    result = store.write_conditional("k", "should not write", etag='"wrong"')
    assert result is False
    assert store.read("k") == "original"


# ── delete ─────────────────────────────────────────────────────────────────────

def test_delete_removes_key(store):
    store.write("k", "v")
    store.delete("k")
    assert store.read("k") is None


def test_delete_missing_is_noop(store):
    store.delete("never/existed")  # must not raise


def test_delete_clears_etag(store):
    store.write("k", "v")
    store.delete("k")
    content, etag = store.read_with_etag("k")
    assert content is None
    assert etag is None


# ── list ───────────────────────────────────────────────────────────────────────

def test_list_empty(store):
    assert store.list("prefix/") == []


def test_list_returns_matching_keys(store):
    store.write("site/page/content.md", "a")
    store.write("site/page/.agents/foo/agent.md", "b")
    store.write("other/content.md", "c")
    keys = store.list("site/page/")
    assert set(keys) == {"site/page/content.md", "site/page/.agents/foo/agent.md"}


def test_list_exact_prefix_match(store):
    store.write("abc", "1")
    store.write("abcd", "2")
    store.write("xyz", "3")
    assert store.list("abc") == ["abc", "abcd"] or set(store.list("abc")) == {"abc", "abcd"}
