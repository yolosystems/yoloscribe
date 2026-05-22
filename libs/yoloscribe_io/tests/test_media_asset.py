import json
import pytest

from yoloscribe_io.events import EventType
from yoloscribe_io.storage import LocalStorageBackend
from yoloscribe_io.media_asset import (
    MediaAsset,
    list_page_media,
    load_media_asset,
)


# ── helpers ───────────────────────────────────────────────────────────────────

class CapturingHandler:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


@pytest.fixture
def store():
    return LocalStorageBackend()


def _asset(store, *, site="s", page_path="blog", filename="photo.jpg",
           mime_type="image/jpeg", size_bytes=1024, cdn_url="https://cdn.example.com/photo.jpg"):
    return MediaAsset(
        site, page_path, filename, store,
        mime_type=mime_type, size_bytes=size_bytes, cdn_url=cdn_url,
    )


# ── properties ────────────────────────────────────────────────────────────────

def test_site_property(store):
    a = _asset(store)
    assert a.site == "s"


def test_page_path_property(store):
    a = _asset(store)
    assert a.page_path == "blog"


def test_filename_property(store):
    a = _asset(store)
    assert a.filename == "photo.jpg"


def test_mime_type_property(store):
    a = _asset(store)
    assert a.mime_type == "image/jpeg"


def test_size_bytes_property(store):
    a = _asset(store)
    assert a.size_bytes == 1024


def test_cdn_url_property(store):
    a = _asset(store)
    assert a.cdn_url == "https://cdn.example.com/photo.jpg"


# ── key ───────────────────────────────────────────────────────────────────────

def test_key_with_page_path(store):
    a = _asset(store, site="mysite", page_path="docs", filename="fig.png")
    assert a.key == "mysite/docs/.media/fig.png.json"


def test_key_without_page_path(store):
    a = MediaAsset("mysite", "", "root.png", store)
    assert a.key == "mysite/.media/root.png.json"


def test_key_nested_page_path(store):
    a = MediaAsset("s", "blog/posts", "hero.jpg", store)
    assert a.key == "s/blog/posts/.media/hero.jpg.json"


# ── register ──────────────────────────────────────────────────────────────────

def test_register_writes_metadata(store):
    a = _asset(store)
    a.register()
    raw = store.read(a.key)
    assert raw is not None
    d = json.loads(raw)
    assert d["filename"] == "photo.jpg"
    assert d["mime_type"] == "image/jpeg"
    assert d["size_bytes"] == 1024
    assert d["cdn_url"] == "https://cdn.example.com/photo.jpg"


def test_register_creates_exists(store):
    a = _asset(store)
    assert not a.exists()
    a.register()
    assert a.exists()


def test_register_emits_media_added(store):
    a = _asset(store)
    cap = CapturingHandler()
    a.add_handler(cap)
    a.register()
    assert cap.events[0].type == EventType.PAGE_MEDIA_ADDED


def test_register_event_payload_fields(store):
    a = _asset(store, site="s", page_path="blog", filename="photo.jpg",
               mime_type="image/jpeg", size_bytes=512, cdn_url="https://cdn.example.com/x")
    cap = CapturingHandler()
    a.add_handler(cap)
    a.register()
    ev = cap.events[0]
    assert ev.payload["site"] == "s"
    assert ev.payload["page_path"] == "blog"
    assert ev.payload["filename"] == "photo.jpg"
    assert ev.payload["mime_type"] == "image/jpeg"
    assert ev.payload["size_bytes"] == 512
    assert ev.payload["cdn_url"] == "https://cdn.example.com/x"


def test_register_overwrites_existing(store):
    a = _asset(store, size_bytes=100)
    a.register()
    b = MediaAsset("s", "blog", "photo.jpg", store, size_bytes=200)
    b.register()
    raw = store.read(a.key)
    assert json.loads(raw)["size_bytes"] == 200


# ── remove ────────────────────────────────────────────────────────────────────

def test_remove_deletes_metadata(store):
    a = _asset(store)
    a.register()
    a.remove()
    assert not a.exists()


def test_remove_emits_media_removed(store):
    a = _asset(store)
    a.register()
    cap = CapturingHandler()
    a.add_handler(cap)
    a.remove()
    assert cap.events[0].type == EventType.PAGE_MEDIA_REMOVED


def test_remove_event_payload(store):
    a = _asset(store)
    a.register()
    cap = CapturingHandler()
    a.add_handler(cap)
    a.remove()
    ev = cap.events[0]
    assert ev.payload["site"] == "s"
    assert ev.payload["page_path"] == "blog"
    assert ev.payload["filename"] == "photo.jpg"


def test_remove_nonexistent_does_not_raise(store):
    a = _asset(store)
    a.remove()  # must not raise


# ── exists ────────────────────────────────────────────────────────────────────

def test_exists_false_when_not_registered(store):
    a = _asset(store)
    assert not a.exists()


def test_exists_true_after_register(store):
    a = _asset(store)
    a.register()
    assert a.exists()


def test_exists_false_after_remove(store):
    a = _asset(store)
    a.register()
    a.remove()
    assert not a.exists()


# ── load_media_asset ──────────────────────────────────────────────────────────

def test_load_returns_none_when_absent(store):
    assert load_media_asset("s", "blog", "photo.jpg", store) is None


def test_load_returns_asset(store):
    a = _asset(store, mime_type="image/png", size_bytes=2048, cdn_url="https://cdn.x/img.png")
    a.register()
    loaded = load_media_asset("s", "blog", "photo.jpg", store)
    assert loaded is not None
    assert loaded.mime_type == "image/png"
    assert loaded.size_bytes == 2048
    assert loaded.cdn_url == "https://cdn.x/img.png"


def test_load_sets_correct_filename(store):
    a = _asset(store, filename="banner.gif")
    a.register()
    loaded = load_media_asset("s", "blog", "banner.gif", store)
    assert loaded.filename == "banner.gif"


def test_load_sets_correct_page_path(store):
    a = _asset(store, page_path="news")
    a.register()
    loaded = load_media_asset("s", "news", "photo.jpg", store)
    assert loaded.page_path == "news"


def test_load_malformed_json_returns_none(store):
    store.write("s/blog/.media/photo.jpg.json", "bad json {")
    assert load_media_asset("s", "blog", "photo.jpg", store) is None


# ── list_page_media ───────────────────────────────────────────────────────────

def test_list_page_media_empty(store):
    assert list_page_media("s", "blog", store) == []


def test_list_page_media_returns_registered_assets(store):
    MediaAsset("s", "blog", "a.jpg", store, mime_type="image/jpeg").register()
    MediaAsset("s", "blog", "b.png", store, mime_type="image/png").register()
    assets = list_page_media("s", "blog", store)
    filenames = {a.filename for a in assets}
    assert "a.jpg" in filenames
    assert "b.png" in filenames


def test_list_page_media_count(store):
    for name in ("x.jpg", "y.jpg", "z.png"):
        MediaAsset("s", "blog", name, store).register()
    assert len(list_page_media("s", "blog", store)) == 3


def test_list_page_media_scoped_to_page(store):
    MediaAsset("s", "blog", "a.jpg", store).register()
    MediaAsset("s", "docs", "b.jpg", store).register()
    assert len(list_page_media("s", "blog", store)) == 1


def test_list_page_media_ignores_non_json(store):
    store.write("s/blog/.media/other.txt", "not an asset")
    MediaAsset("s", "blog", "real.jpg", store).register()
    assets = list_page_media("s", "blog", store)
    assert len(assets) == 1
    assert assets[0].filename == "real.jpg"


def test_list_page_media_skips_malformed(store):
    store.write("s/blog/.media/bad.json", "not json {")
    MediaAsset("s", "blog", "good.jpg", store).register()
    assets = list_page_media("s", "blog", store)
    filenames = [a.filename for a in assets]
    assert "good.jpg" in filenames
    assert "bad" not in filenames
