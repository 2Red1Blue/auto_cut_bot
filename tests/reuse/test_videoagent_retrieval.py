"""R2A retrieval unit tests: deterministic lexical index, manifest binding,
stale/tampered detection, duplicate document refusal, Recall@K metrics."""

from __future__ import annotations

import pytest

from tools.reuse.models import json_sha256
from tools.reuse.videoagent_retrieval.lexical_index import (
    LexicalIndex,
    verify_hit_source,
)
from tools.reuse.videoagent_retrieval.projection import (
    IndexDocument,
    project_candidate,
    project_event,
)
from tools.reuse.window_memory.evaluator import evaluate_retrieval
from tools.reuse.window_memory.event_deduper import EventObservation


def _doc(ref: str, text: str, episode: int = 1) -> IndexDocument:
    # content hash derived from the source text payload, as real projections do
    return IndexDocument(
        object_ref=ref, content_hash=json_sha256({"text": text}), text=text,
        kind="event", episode=episode, source_refs=(f"src-{ref}",),
    )


class TestProjection:
    def test_event_projection_binds_content_hash(self) -> None:
        event = EventObservation(ref="ev1", episode=1, window=1,
                                 summary="The key is revealed", participants=("Lucifer",),
                                 source_refs=("s1",))
        doc = project_event(event)
        assert doc.object_ref == "ev1"
        assert "revealed" in doc.text and "Lucifer" in doc.text
        # identical input -> identical hash (deterministic)
        assert project_event(event).content_hash == doc.content_hash

    def test_candidate_projection_ignores_unknown_fields_but_binds_them(self) -> None:
        base = {"reason": "strong reveal", "episode": 1, "tags": ["reveal"]}
        doc = project_candidate("c1", base)
        assert "reveal" in doc.text
        with_extra = project_candidate("c1", {**base, "future_field": "x"})
        assert with_extra.text == doc.text  # unknown field not projected...
        assert with_extra.content_hash != doc.content_hash  # ...but still bound into identity


class TestLexicalIndex:
    def test_duplicate_document_refs_refused(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            LexicalIndex([_doc("ev1", "a"), _doc("ev1", "b")])

    def test_empty_index_refused(self) -> None:
        with pytest.raises(ValueError):
            LexicalIndex([])

    def test_english_and_chinese_queries_rank_relevant_first(self) -> None:
        docs = [
            _doc("ev1", "Selene forges the silver pendant in the workshop"),
            _doc("ev2", "Lucifer kneels at the graveyard in the rain"),
            _doc("ev3", "天使守卫袭击了木屋"),
        ]
        index = LexicalIndex(docs)
        hits = index.search("q1", "silver pendant forge", top_k=2)
        assert hits and hits[0].object_ref == "ev1"
        hits_cn = index.search("q2", "天使守卫 木屋", top_k=2)
        assert hits_cn and hits_cn[0].object_ref == "ev3"

    def test_deterministic_ordering(self) -> None:
        docs = [_doc(f"ev{i}", f"reveal story number {i}") for i in range(5)]
        r1 = [h.object_ref for h in LexicalIndex(docs).search("q", "reveal story", 5)]
        r2 = [h.object_ref for h in LexicalIndex(docs).search("q", "reveal story", 5)]
        assert r1 == r2

    def test_manifest_binds_documents_and_params(self) -> None:
        docs = [_doc("ev1", "text one"), _doc("ev2", "text two")]
        manifest = LexicalIndex(docs).manifest
        assert manifest.document_refs == ("ev1", "ev2")
        assert manifest.algorithm == LexicalIndex.ALGORITHM
        other = LexicalIndex([_doc("ev1", "text one"), _doc("ev2", "CHANGED")])
        assert other.manifest.manifest_hash != manifest.manifest_hash

    def test_stale_source_detected_by_verify(self) -> None:
        docs = [_doc("ev1", "the graveyard scene")]
        index = LexicalIndex(docs)
        hit = index.search("q", "graveyard", 1)[0]
        assert verify_hit_source(hit, index.manifest.document_hashes[0], index.manifest) is True
        # source object changed after index build -> hit must be rejected
        assert verify_hit_source(hit, json_sha256({"text": "tampered"}), index.manifest) is False
        # hit referencing an object the manifest never bound -> rejected
        stranger = hit.__class__(query_id="q", rank=1, object_ref="ghost",
                                 score=1.0, support="", reason="")
        assert verify_hit_source(stranger, "hash-ev1", index.manifest) is False

    def test_top_k_bounds_results(self) -> None:
        docs = [_doc(f"ev{i}", f"shared term doc{i}") for i in range(6)]
        assert len(LexicalIndex(docs).search("q", "shared term", 3)) == 3
        with pytest.raises(ValueError):
            LexicalIndex(docs).search("q", "shared", 0)


class TestRecallMetrics:
    def test_recall_at_k_with_denominator(self) -> None:
        results = evaluate_retrieval(
            hits_by_query={"q1": ["ev2", "ev1", "ev3"]},
            relevant_by_query={"q1": {"ev1", "ev2"}}, k=2)
        by_name = {r.metric: r for r in results}
        assert by_name["recall_at_2"].value == 1.0  # both relevant in top 2
        assert by_name["recall_at_2"].denom == 2.0

    def test_empty_relevant_set_null_with_reason(self) -> None:
        results = evaluate_retrieval(
            hits_by_query={"q1": ["ev1"]}, relevant_by_query={"q1": set()}, k=3)
        micro = next(r for r in results if r.metric == "recall_at_3_micro")
        assert micro.value is None and micro.missing_reason

    def test_micro_average_across_queries(self) -> None:
        results = evaluate_retrieval(
            hits_by_query={"q1": ["a"], "q2": ["x", "y", "b"]},
            relevant_by_query={"q1": {"a"}, "q2": {"b"}}, k=1)
        micro = next(r for r in results if r.metric == "recall_at_1_micro")
        assert micro.value == 0.5  # q1 perfect, q2 miss at k=1
