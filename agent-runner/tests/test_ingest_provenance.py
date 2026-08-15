"""The ingest agent's half of provenance (YOL-552).

The record is only worth anything if the agent actually reads the owner's intent
before routing, and actually lands the record on the page it chose. Both are
easy to lose silently: the intent is advisory (nothing fails if it is ignored)
and the landing is a side effect of marking a file processed (nothing fails if
it is skipped). So both get pinned here.

`page_path` is optional on `ingest_mark_processed` because a file can legitimately
be processed without being written anywhere — flagged to the owner, or skipped.
That optionality is exactly what makes "the agent just never passes it" a
plausible regression, hence the explicit test for the negative case too.
"""
from __future__ import annotations

from yoloscribe_io import Provenance, SourceStatus
from yoloscribe_io.storage import LocalStorageBackend

from agent_runner.agents.search import NullSearchBackend
from agent_runner.mcp_client import FakeMCPClient, StorageMCPClient


def _agent(mcp, **kw):
    """A bare IngestAgent over a fake client.

    Constructed through __new__ rather than the real __init__ so the test does
    not need a model, a tool surface, or a full agent definition to exercise
    three methods of plumbing.
    """
    from agent_runner.agents.ingest import IngestAgent

    agent = IngestAgent.__new__(IngestAgent)
    agent._mcp = mcp
    agent._site = "s"
    agent._extractors = {}
    for k, v in kw.items():
        setattr(agent, k, v)
    return agent


class TestReadIntent:
    def test_returns_the_owners_stated_intent(self):
        mcp = FakeMCPClient(provenance={
            "q3.pdf": {"intent": "file under planning", "source_url": "https://sp/q3.pdf"},
        })
        out = _agent(mcp).ingest_read_intent("q3.pdf")
        assert "file under planning" in out
        assert "https://sp/q3.pdf" in out

    def test_missing_record_tells_the_agent_to_use_its_own_judgement(self):
        """Silence must read as 'decide for yourself', not as an error to retry."""
        out = _agent(FakeMCPClient()).ingest_read_intent("unknown.pdf")
        assert "No stated intent" in out

    def test_intent_without_a_source_omits_the_source_line(self):
        mcp = FakeMCPClient(provenance={"a.md": {"intent": "notes", "source_url": ""}})
        out = _agent(mcp).ingest_read_intent("a.md")
        assert "notes" in out
        assert "Source:" not in out

    def test_leading_slash_is_tolerated(self):
        mcp = FakeMCPClient(provenance={"a.md": {"intent": "notes", "source_url": ""}})
        assert "notes" in _agent(mcp).ingest_read_intent("/a.md")


class TestLandingOnMarkProcessed:
    def test_page_path_lands_the_record(self):
        mcp = FakeMCPClient(ingest_files={"q3.pdf": b"x"})
        _agent(mcp).ingest_mark_processed("q3.pdf", "planning/q3")

        assert mcp.landed_provenance == [{
            "filename": "q3.pdf", "page_path": "planning/q3",
            "extractor": "native-text", "retention": "",
        }]

    def test_no_page_path_lands_nothing(self):
        """A file flagged to the owner or skipped was routed nowhere, so there is
        no page for its provenance to live on."""
        mcp = FakeMCPClient(ingest_files={"q3.pdf": b"x"})
        _agent(mcp).ingest_mark_processed("q3.pdf")
        assert mcp.landed_provenance == []

    def test_the_file_still_moves_when_landing_fails(self):
        """Losing provenance is bad; wedging the ingest queue is worse.

        A run that cannot record provenance must still mark the file processed,
        or the next run reprocesses it forever.
        """
        mcp = FakeMCPClient(ingest_files={"q3.pdf": b"x"})

        def boom(*a, **k):
            raise RuntimeError("provenance backend down")

        mcp.ingest_record_provenance = boom
        out = _agent(mcp).ingest_mark_processed("q3.pdf", "planning/q3")
        assert "Marked as processed" in out

    def test_extractor_is_recorded_per_file(self):
        """One run handles many documents; extraction fidelity is per-document,
        and it decides which of them can be usefully re-extracted later."""
        mcp = FakeMCPClient(ingest_files={"a.pdf": b"x", "b.docx": b"y"})
        agent = _agent(mcp)
        agent._extractors = {"a.pdf": "pypdf", "b.docx": "python-docx"}

        agent.ingest_mark_processed("a.pdf", "p")
        agent.ingest_mark_processed("b.docx", "p")

        assert [r["extractor"] for r in mcp.landed_provenance] == ["pypdf", "python-docx"]


class TestStorageClientRoundTrip:
    """The direct-S3 client must behave like the MCP one, or the two diverge
    depending on an env var (AGENT_RUNNER_ACCESS) rather than on behaviour."""

    def _client(self, store):
        return StorageMCPClient(store, "s", NullSearchBackend(), lambda *a, **k: None, "u1")

    def test_read_then_land_preserves_intent(self):
        from yoloscribe_io import load_media_asset, write_staged

        store = LocalStorageBackend()
        write_staged("s", Provenance(
            filename="q3.pdf", intent="file under planning",
            source_url="https://sp/q3.pdf", source_status=SourceStatus.UNVERIFIED,
        ), store)
        client = self._client(store)

        assert client.ingest_read_provenance("q3.pdf")["intent"] == "file under planning"

        client.ingest_record_provenance("q3.pdf", "planning/q3", extractor="pypdf")

        asset = load_media_asset("s", "planning/q3", "q3.pdf", store)
        assert asset is not None and asset.provenance is not None
        assert asset.provenance.intent == "file under planning"
        assert asset.provenance.source_url == "https://sp/q3.pdf"
        assert asset.provenance.page_path == "planning/q3"

    def test_landing_clears_the_staged_record(self):
        """The staged record is keyed on the queue filename, which stops meaning
        anything once the file is processed."""
        from yoloscribe_io import read_staged, write_staged

        store = LocalStorageBackend()
        write_staged("s", Provenance(filename="q3.pdf", intent="x"), store)
        self._client(store).ingest_record_provenance("q3.pdf", "planning/q3")
        assert read_staged("s", "q3.pdf", store) is None

    def test_landing_without_a_staged_record_still_records_the_routing(self):
        """Files queued before provenance existed still deserve a landed record —
        it just has no intent or source to carry."""
        from yoloscribe_io import load_media_asset

        store = LocalStorageBackend()
        self._client(store).ingest_record_provenance("legacy.md", "notes")

        asset = load_media_asset("s", "notes", "legacy.md", store)
        assert asset is not None and asset.provenance is not None
        assert asset.provenance.page_path == "notes"
        assert asset.provenance.intent == ""
