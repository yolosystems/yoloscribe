"""Ingest provenance: the record, its lifecycle, and its access-gating rule (YOL-552).

Provenance is write-once-or-never — the source of a document is unrecoverable
after the upload, so a bug that silently drops a field produces documents that
are permanently second-class rather than temporarily wrong. These tests are
weighted accordingly: most of them are about *not losing* data across the staged
→ landed transition and across a storage round trip.

`gates_access` gets its own class because YOL-553 will hang an access-control
decision on it, and the safe answer (False) is the one a bug is least likely to
produce accidentally.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from yoloscribe_io import (
    MediaAsset,
    Provenance,
    Retention,
    SourceStatus,
    delete_staged,
    load_media_asset,
    read_staged,
    write_staged,
)
from yoloscribe_io.storage import LocalStorageBackend


def _staged(**kw) -> Provenance:
    base = dict(
        filename="q3-plan.pdf",
        intent="filing the Q3 board deck under planning",
        source_url="https://contoso.sharepoint.com/sites/fin/q3-plan.pdf",
        source_status=SourceStatus.UNVERIFIED,
        ingested_by="u1",
    )
    base.update(kw)
    return Provenance(**base)


class TestRecord:
    def test_from_dict_ignores_unknown_keys(self):
        """These records outlive the code that wrote them.

        A field added in a later version must not make an older record
        unreadable, and vice versa — the failure would be silent data loss on
        exactly the data that cannot be regenerated.
        """
        prov = Provenance.from_dict({
            "filename": "a.pdf", "intent": "why", "invented_later": "???",
        })
        assert prov.filename == "a.pdf"
        assert prov.intent == "why"

    @pytest.mark.parametrize("bad", [None, {}])
    def test_from_dict_tolerates_empty(self, bad):
        assert Provenance.from_dict(bad).filename == ""

    def test_ingested_at_is_populated(self):
        assert Provenance().ingested_at


class TestLanding:
    def test_land_preserves_every_staged_field(self):
        """The landing transition is where provenance would get silently dropped.

        Asserted field-by-field rather than on one representative, because the
        whole value of the record is that no part of it is lost in the hop from
        the queue to the page.
        """
        staged = _staged()
        landed = staged.land(page_path="planning/q3", extractor="pypdf")

        assert landed.filename == staged.filename
        assert landed.intent == staged.intent
        assert landed.source_url == staged.source_url
        assert landed.source_status == staged.source_status
        assert landed.ingested_at == staged.ingested_at
        assert landed.ingested_by == staged.ingested_by

    def test_land_adds_the_routing_outcome(self):
        landed = _staged().land(page_path="planning/q3", extractor="pypdf")
        assert landed.page_path == "planning/q3"
        assert landed.extractor == "pypdf"
        assert landed.routed_at

    def test_land_defaults_retention_to_keeping_the_original(self):
        """Deleting the bytes forfeits re-extraction forever, so it is never the default."""
        assert _staged().land(page_path="p").retention == Retention.YOLOSCRIBE
        assert Retention.DEFAULT != Retention.DELETE

    def test_land_honours_an_explicit_retention(self):
        landed = _staged().land(page_path="p", retention=Retention.DELETE)
        assert landed.retention == Retention.DELETE

    def test_land_rejects_an_unknown_retention(self):
        """A typo must not silently become a retention policy."""
        with pytest.raises(ValueError, match="Unknown retention"):
            _staged().land(page_path="p", retention="discard")

    def test_land_does_not_mutate_the_staged_record(self):
        staged = _staged()
        staged.land(page_path="planning/q3")
        assert staged.page_path == ""


class TestGatesAccess:
    """Only a verified source may gate access (YOL-553).

    `source_url` is an assertion by whoever ingested the document. Treating an
    unchecked claim as an access anchor would make naming a benign public URL an
    easier bypass than naming none at all.
    """

    def test_verified_source_gates(self):
        assert _staged(source_status=SourceStatus.VERIFIED).gates_access

    @pytest.mark.parametrize("status", [
        SourceStatus.NONE, SourceStatus.UNVERIFIED, SourceStatus.MISMATCH,
    ])
    def test_everything_else_does_not(self, status):
        assert not _staged(source_status=status).gates_access

    def test_verified_but_empty_url_does_not_gate(self):
        """A status with nothing to check against is not a gate."""
        assert not _staged(source_url="", source_status=SourceStatus.VERIFIED).gates_access


class TestStagedIO:
    def test_round_trip(self):
        store = LocalStorageBackend()
        write_staged("s", _staged(), store)
        got = read_staged("s", "q3-plan.pdf", store)
        assert got is not None
        assert got.intent == "filing the Q3 board deck under planning"
        assert got.source_url.endswith("q3-plan.pdf")

    def test_missing_record_is_none_not_an_error(self):
        """Files queued before provenance existed, or dropped in by other paths,
        simply have none — that is normal, not a failure."""
        assert read_staged("s", "never-seen.pdf", LocalStorageBackend()) is None

    def test_malformed_record_is_none_not_a_crash(self):
        store = LocalStorageBackend()
        store.write("s/.user/ingest/.provenance/broken.pdf.json", "{not json")
        assert read_staged("s", "broken.pdf", store) is None

    def test_staged_records_live_outside_the_queue_listing(self):
        """They must not look like documents awaiting routing."""
        store = LocalStorageBackend()
        write_staged("s", _staged(), store)
        keys = list(store.list("s/.user/ingest/"))
        assert all("/.provenance/" in k for k in keys)

    def test_delete_is_idempotent(self):
        store = LocalStorageBackend()
        write_staged("s", _staged(), store)
        delete_staged("s", "q3-plan.pdf", store)
        delete_staged("s", "q3-plan.pdf", store)  # must not raise
        assert read_staged("s", "q3-plan.pdf", store) is None


class TestMediaAssetCarriesProvenance:
    def test_register_and_reload(self):
        store = LocalStorageBackend()
        landed = _staged().land(page_path="planning/q3", extractor="pypdf")
        MediaAsset(
            site="s", page_path="planning/q3", filename="q3-plan.pdf",
            storage=store, provenance=landed,
        ).register()

        asset = load_media_asset("s", "planning/q3", "q3-plan.pdf", store)
        assert asset is not None and asset.provenance is not None
        assert asset.provenance.intent == landed.intent
        assert asset.provenance.source_url == landed.source_url
        assert asset.provenance.extractor == "pypdf"

    def test_asset_without_provenance_reloads_as_none(self):
        """Assets attached straight to a page have no source beyond whoever
        attached them, and must not acquire an empty record."""
        store = LocalStorageBackend()
        MediaAsset(
            site="s", page_path="p", filename="diagram.png",
            storage=store, mime_type="image/png",
        ).register()

        asset = load_media_asset("s", "p", "diagram.png", store)
        assert asset is not None and asset.provenance is None

    def test_provenance_is_nested_not_flattened(self):
        """Nesting is what stops a provenance field colliding with a media field,
        and makes `"provenance" in record` the test for 'arrived via ingest'."""
        store = LocalStorageBackend()
        MediaAsset(
            site="s", page_path="p", filename="a.pdf", storage=store,
            mime_type="application/pdf",
            provenance=_staged(filename="a.pdf").land(page_path="p"),
        ).register()

        raw = json.loads(store.read("s/p/.media/a.pdf.json"))
        assert "provenance" in raw
        assert raw["mime_type"] == "application/pdf"
        assert raw["provenance"]["intent"]
