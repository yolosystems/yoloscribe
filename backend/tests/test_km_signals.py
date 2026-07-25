"""Unit tests for the YoloScribe→KM signal mapping (YOL-492).

These pin the two things that carry semantic weight for YoloBrain clustering:
the page_type derived from a path's topic hierarchy, and the (signal_type,
params) shape each mutation produces.
"""
from __future__ import annotations

import km_signals


class TestDerivePageType:
    def test_leaf_uses_immediate_parent_segment(self):
        assert (
            km_signals.derive_page_type("projects/yoloscribe/feature-backlog/native-signal")
            == "feature-backlog"
        )

    def test_parent_segment_is_singularized(self):
        assert km_signals.derive_page_type("accounts/acme") == "account"
        assert km_signals.derive_page_type("projects/yoloscribe") == "project"

    def test_top_level_page_uses_own_slug(self):
        assert km_signals.derive_page_type("notes") == "note"

    def test_root_page_is_generic(self):
        assert km_signals.derive_page_type("") == "page"
        assert km_signals.derive_page_type("/") == "page"

    def test_dot_system_segments_are_skipped(self):
        assert km_signals.derive_page_type(".user/ingest") == "ingest"

    def test_double_s_word_not_over_singularized(self):
        assert km_signals.derive_page_type("class") == "class"


class TestParseSections:
    def test_extracts_atx_headings_in_order(self):
        md = "# Title\n\n## Alpha\n\ntext\n\n### Beta\n\n## Gamma\n"
        assert km_signals.parse_sections(md) == ["Title", "Alpha", "Beta", "Gamma"]

    def test_empty_or_none_is_empty_list(self):
        assert km_signals.parse_sections("") == []
        assert km_signals.parse_sections(None) == []

    def test_ignores_non_heading_hashes(self):
        assert km_signals.parse_sections("no headings here\n#notaheading\n") == []


class TestBuilders:
    def test_page_structured(self):
        sig_type, params = km_signals.page_structured_signal(
            "projects/x", "# X\n\n## Overview\n\n## Tasks\n"
        )
        assert sig_type == "page_structured"
        assert params == {
            "page_type": "project",
            "format": "markdown",
            "sections": ["X", "Overview", "Tasks"],
            "target": {"system": "yoloscribe", "path": "projects/x"},
        }

    def test_content_routed_is_replace(self):
        sig_type, params = km_signals.content_routed_signal("accounts/acme")
        assert sig_type == "content_routed"
        assert params["integration"] == "replace"
        assert params["page_type"] == "account"
        assert params["target"] == {"system": "yoloscribe", "path": "accounts/acme"}

    def test_agent_provisioned(self):
        sig_type, params = km_signals.agent_provisioned_signal(
            "projects/x", "page", ["linear"], "on_write"
        )
        assert sig_type == "agent_provisioned"
        assert params == {
            "page_type": "project",
            "agent_type": "page",
            "skills": ["linear"],
            "trigger": "on_write",
            "host": {"path": "projects/x"},
        }

    def test_agent_provisioned_tolerates_none_skills(self):
        _, params = km_signals.agent_provisioned_signal("", "notification", None, "on_notify")
        assert params["skills"] == []
        assert params["page_type"] == "page"

    def test_notification_sent_with_page(self):
        sig_type, params = km_signals.notification_sent_signal("page_shared", "projects/x")
        assert sig_type == "notification_sent"
        assert params == {
            "channel": "notifications",
            "event": "page_shared",
            "page_type": "project",
            "target": {"system": "yoloscribe", "path": "projects/x"},
        }

    def test_notification_sent_site_level_omits_page_fields(self):
        _, params = km_signals.notification_sent_signal("access_requested")
        assert params == {"channel": "notifications", "event": "access_requested"}
        assert "page_type" not in params
        assert "target" not in params

    def test_proposal_accepted(self):
        sig_type, params = km_signals.proposal_accepted_signal("projects/x")
        assert sig_type == "proposal_accepted"
        assert params == {
            "what": "content change",
            "page_type": "project",
            "target": {"system": "yoloscribe", "path": "projects/x"},
        }

    def test_proposal_rejected_has_required_empty_correction(self):
        sig_type, params = km_signals.proposal_rejected_signal("accounts/acme")
        assert sig_type == "proposal_rejected"
        assert params == {
            "what": "content change",
            "correction": "",
            "page_type": "account",
            "target": {"system": "yoloscribe", "path": "accounts/acme"},
        }

    def test_notification_suppressed_with_page(self):
        sig_type, params = km_signals.notification_suppressed_signal(
            event="page_shared", reason="owner already notified", page_path="projects/x"
        )
        assert sig_type == "notification_suppressed"
        assert params == {
            "channel": "notifications",
            "event": "page_shared",
            "reason": "owner already notified",
            "page_type": "project",
            "target": {"system": "yoloscribe", "path": "projects/x"},
        }

    def test_notification_suppressed_site_level_omits_page_fields(self):
        _, params = km_signals.notification_suppressed_signal("access_requested", "duplicate")
        assert params == {
            "channel": "notifications",
            "event": "access_requested",
            "reason": "duplicate",
        }

    def test_user_instruction_defaults_domain(self):
        sig_type, params = km_signals.user_instruction_signal("always link the source")
        assert sig_type == "user_instruction"
        assert params == {"instruction": "always link the source", "domain": "general"}

    def test_user_instruction_explicit_domain(self):
        _, params = km_signals.user_instruction_signal("cite inline", domain="present")
        assert params["domain"] == "present"
