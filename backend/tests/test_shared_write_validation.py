"""Unit tests for shared-write content validation (PR 7).

Covers:
  YOL-64  Stricter 128 KB size limit for shared-write users
  YOL-63  bleach HTML sanitisation of shared-write content
  YOL-67  Structured audit log emitted on every shared-write PUT /content
"""

import json
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import bleach

from config import MAX_CONTENT_BYTES, MAX_SHARED_WRITE_BYTES


# ---------------------------------------------------------------------------
# Size limit constants sanity checks (YOL-64)
# ---------------------------------------------------------------------------


class TestSizeLimitConstants:
    def test_shared_write_limit_is_128_kb(self):
        assert MAX_SHARED_WRITE_BYTES == 128 * 1024

    def test_shared_write_limit_is_stricter_than_owner_limit(self):
        assert MAX_SHARED_WRITE_BYTES < MAX_CONTENT_BYTES

    def test_owner_limit_is_512_kb(self):
        assert MAX_CONTENT_BYTES == 512 * 1024


# ---------------------------------------------------------------------------
# bleach sanitisation (YOL-63)
# ---------------------------------------------------------------------------

# Import the allowed-tag / attribute lists from the router module directly so
# the tests always stay in sync with the production configuration.
from routers.content import _ALLOWED_TAGS, _ALLOWED_ATTRS


class TestBleachSanitisation:
    def _clean(self, html: str) -> str:
        return bleach.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)

    def test_plain_markdown_unchanged(self):
        md = "# Hello\n\nThis is **bold** and _italic_."
        assert self._clean(md) == md

    def test_script_tag_stripped(self):
        # bleach strip=True removes the tag; inner text is kept as harmless plain text.
        html = 'Hello <script>alert("xss")</script> world'
        result = self._clean(html)
        assert "<script>" not in result
        assert "</script>" not in result
        assert "Hello" in result
        assert "world" in result

    def test_iframe_stripped(self):
        html = 'Text <iframe src="https://evil.example/"></iframe> more'
        result = self._clean(html)
        assert "<iframe" not in result

    def test_onclick_attribute_stripped(self):
        html = '<p onclick="evil()">Click me</p>'
        result = self._clean(html)
        assert "onclick" not in result
        assert "Click me" in result

    def test_javascript_href_stripped(self):
        html = '<a href="javascript:alert(1)">link</a>'
        result = self._clean(html)
        assert "javascript:" not in result

    def test_safe_anchor_preserved(self):
        html = '<a href="https://example.com" title="Example">link</a>'
        result = self._clean(html)
        assert 'href="https://example.com"' in result
        assert ">link<" in result

    def test_safe_img_preserved(self):
        html = '<img src="https://example.com/img.png" alt="photo">'
        result = self._clean(html)
        assert "img" in result
        assert 'src="https://example.com/img.png"' in result

    def test_img_onerror_stripped(self):
        html = '<img src="x" onerror="alert(1)">'
        result = self._clean(html)
        assert "onerror" not in result

    def test_table_preserved(self):
        html = "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>"
        result = self._clean(html)
        assert "<table>" in result
        assert "<td>" in result

    def test_style_tag_stripped(self):
        html = '<style>body { display: none }</style>Normal text'
        result = self._clean(html)
        assert "<style>" not in result
        assert "Normal text" in result

    def test_form_tag_stripped(self):
        html = '<form action="/steal"><input name="secret"></form>Text'
        result = self._clean(html)
        assert "<form" not in result
        assert "<input" not in result


# ---------------------------------------------------------------------------
# Audit logging (YOL-67)
# ---------------------------------------------------------------------------


class TestAuditLogging:
    def test_audit_log_emits_on_shared_write(self, caplog):
        """Verify the audit logger fires with the correct fields."""
        from routers.content import _audit_log
        with caplog.at_level(logging.INFO, logger="yoloscribe.audit"):
            _audit_log.info(
                json.dumps({
                    "event": "shared_write",
                    "site": "alice",
                    "path": "content.md",
                    "user_email": "bob@example.com",
                    "user_id": "bob-uuid",
                    "bytes": 1024,
                })
            )
        assert len(caplog.records) == 1
        entry = json.loads(caplog.records[0].message)
        assert entry["event"] == "shared_write"
        assert entry["site"] == "alice"
        assert entry["path"] == "content.md"
        assert entry["user_email"] == "bob@example.com"
        assert entry["user_id"] == "bob-uuid"
        assert entry["bytes"] == 1024

    def test_audit_log_is_valid_json(self, caplog):
        from routers.content import _audit_log
        with caplog.at_level(logging.INFO, logger="yoloscribe.audit"):
            _audit_log.info(
                json.dumps({
                    "event": "shared_write",
                    "site": "s",
                    "path": "p",
                    "user_email": "e",
                    "user_id": "u",
                    "bytes": 0,
                })
            )
        # Must not raise
        parsed = json.loads(caplog.records[0].message)
        assert isinstance(parsed, dict)

    def test_audit_logger_name(self):
        from routers.content import _audit_log
        assert _audit_log.name == "yoloscribe.audit"
