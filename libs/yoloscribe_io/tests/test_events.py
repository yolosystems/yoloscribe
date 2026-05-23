import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from yoloscribe_io.events import (
    Event,
    EventEmitter,
    EventHandler,
    EventType,
    LoggerEventHandler,
)
from yoloscribe_io.storage import LocalStorageBackend
from yoloscribe_io.markdown_file import MarkdownFile


# ── helpers ───────────────────────────────────────────────────────────────────

class CapturingHandler(EventHandler):
    def __init__(self):
        self.events: list[Event] = []
        self.calls = 0

    def handle(self, event: Event) -> None:
        self.events.append(event)
        self.calls += 1


class RaisingHandler(EventHandler):
    def handle(self, event: Event) -> None:
        raise RuntimeError("boom")


# ── Event dataclass ───────────────────────────────────────────────────────────

def test_event_type_and_payload():
    e = Event(type="page.written", payload={"key": "s/p.md"})
    assert e.type == "page.written"
    assert e.payload == {"key": "s/p.md"}


def test_event_payload_defaults_to_empty_dict():
    e = Event(type="page.read")
    assert e.payload == {}


def test_event_timestamp_is_utc_datetime():
    e = Event(type="page.read")
    assert isinstance(e.timestamp, datetime)
    assert e.timestamp.tzinfo == timezone.utc


def test_event_timestamp_can_be_overridden():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    e = Event(type="page.read", timestamp=t)
    assert e.timestamp == t


# ── LoggerEventHandler ────────────────────────────────────────────────────────

def test_logger_handler_calls_info(caplog):
    handler = LoggerEventHandler()
    with caplog.at_level(logging.INFO, logger="yoloscribe_io.events"):
        handler.handle(Event(type="page.written", payload={"key": "s/p.md"}))
    assert "page.written" in caplog.text


def test_logger_handler_accepts_custom_logger():
    mock_log = MagicMock()
    handler = LoggerEventHandler(log=mock_log)
    handler.handle(Event(type="page.read"))
    mock_log.info.assert_called_once()
    args = mock_log.info.call_args[0]
    assert "page.read" in args[1]


# ── EventEmitter ──────────────────────────────────────────────────────────────

def test_emitter_has_default_logger_handler():
    emitter = EventEmitter()
    assert len(emitter._handlers) == 1
    assert isinstance(emitter._handlers[0], LoggerEventHandler)


def test_emitter_add_handler():
    emitter = EventEmitter()
    cap = CapturingHandler()
    emitter.add_handler(cap)
    assert cap in emitter._handlers


def test_emitter_remove_handler():
    emitter = EventEmitter()
    cap = CapturingHandler()
    emitter.add_handler(cap)
    emitter.remove_handler(cap)
    assert cap not in emitter._handlers


def test_emitter_remove_missing_handler_raises():
    emitter = EventEmitter()
    cap = CapturingHandler()
    with pytest.raises(ValueError):
        emitter.remove_handler(cap)


def test_emit_calls_all_handlers():
    emitter = EventEmitter()
    cap1 = CapturingHandler()
    cap2 = CapturingHandler()
    emitter.add_handler(cap1)
    emitter.add_handler(cap2)
    emitter._emit("page.written", {"key": "k"})
    assert cap1.calls == 1
    assert cap2.calls == 1


def test_emit_passes_correct_event():
    emitter = EventEmitter()
    cap = CapturingHandler()
    emitter.add_handler(cap)
    emitter._emit("page.read", {"key": "site/path.md"})
    assert cap.events[0].type == "page.read"
    assert cap.events[0].payload == {"key": "site/path.md"}


def test_emit_empty_payload_becomes_empty_dict():
    emitter = EventEmitter()
    cap = CapturingHandler()
    emitter.add_handler(cap)
    emitter._emit("page.read")
    assert cap.events[0].payload == {}


def test_raising_handler_does_not_prevent_subsequent_handlers():
    emitter = EventEmitter()
    emitter.add_handler(RaisingHandler())
    cap = CapturingHandler()
    emitter.add_handler(cap)
    emitter._emit("page.written")
    assert cap.calls == 1


def test_raising_handler_is_logged(caplog):
    emitter = EventEmitter()
    emitter.add_handler(RaisingHandler())
    with caplog.at_level(logging.ERROR, logger="yoloscribe_io.events"):
        emitter._emit("page.written")
    assert "boom" in caplog.text


# ── EventType constants ───────────────────────────────────────────────────────

def test_event_type_constants_are_dot_namespaced():
    for attr in vars(EventType).values():
        if isinstance(attr, str):
            assert "." in attr, f"EventType.{attr!r} missing namespace"


# ── MarkdownFile event integration ───────────────────────────────────────────

@pytest.fixture
def store():
    return LocalStorageBackend()


def test_markdown_file_is_event_emitter(store):
    f = MarkdownFile("s", "p/content.md", store)
    assert isinstance(f, EventEmitter)


def test_markdown_file_has_default_logger_handler(store):
    f = MarkdownFile("s", "p/content.md", store)
    assert any(isinstance(h, LoggerEventHandler) for h in f._handlers)


def test_markdown_file_write_emits_page_written(store):
    f = MarkdownFile("s", "p/content.md", store)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.write("# Hello")
    assert cap.calls == 1
    assert cap.events[0].type == EventType.PAGE_WRITTEN
    assert cap.events[0].payload["key"] == "s/p/content.md"


def test_markdown_file_read_emits_page_read(store):
    store.write("s/p/content.md", "data")
    f = MarkdownFile("s", "p/content.md", store)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.read()
    assert cap.calls == 1
    assert cap.events[0].type == EventType.PAGE_READ
    assert cap.events[0].payload["key"] == "s/p/content.md"


def test_markdown_file_handlers_are_independent_across_instances(store):
    f1 = MarkdownFile("s", "a.md", store)
    f2 = MarkdownFile("s", "b.md", store)
    cap = CapturingHandler()
    f1.add_handler(cap)
    f2.write("x")
    assert cap.calls == 0
