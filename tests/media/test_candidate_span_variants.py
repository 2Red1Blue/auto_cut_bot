"""Bounded canonical variants over the complete candidate-local relation."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from autocut_kernel.media.types import TickRange, canonical_sha256
from autocut_kernel.physical_edit.candidate_exact_span import (
    CandidateExactSpanResult,
    compile_candidate_av_span,
    compile_candidate_av_span_variants,
)
from autocut_kernel.physical_edit.exact_span import (
    CandidatePairLimitError,
    ExactSpanValidationError,
)
from autocut_kernel.physical_edit.span_variant_set import (
    SpanVariantEntry,
    SpanVariantSet,
    SpanVariantSetError,
    SpanVariantSetPolicy,
    decode_span_variant_set,
    decode_span_variant_set_json,
    encode_span_variant_set_json,
)

from tests.media.test_candidate_exact_span import _case, _oracle
from tests.pipeline.test_production_recipe import _query


def _query_for(request, result: CandidateExactSpanResult):
    return replace(
        _query(),
        request=request,
        timed_evidence_sha256=result.dialogue_guard.candidate_evidence_sha256,
    )


def _variant_set(*, max_variants: int = 3, narrow: bool = False) -> SpanVariantSet:
    request, root, candidate, plan, profile, clock, policy = _case()
    if narrow:
        request = replace(
            request,
            desired_video_range=replace(
                request.desired_video_range,
                tick_range=TickRange(39, 61),
            ),
        )
    results = compile_candidate_av_span_variants(
        request,
        root,
        candidate,
        plan,
        profile,
        clock,
        policy,
        max_variants=max_variants,
    )
    query = _query_for(request, results[0])
    entry = SpanVariantEntry.from_results(
        ordinal=0,
        story_id=query.story_id,
        beat_id=query.beat_id,
        requirement_id=query.evidence_requirement_id,
        alternative_id=query.alternative_id,
        candidate_id=query.candidate_id,
        query=query,
        results=results,
    )
    return SpanVariantSet(
        SpanVariantSetPolicy(max_variants),
        canonical_sha256({"parent": "request"}),
        canonical_sha256({"parent": "artifact-set"}),
        (entry,),
    )


@pytest.mark.parametrize("max_variants", [1, 3])
@pytest.mark.parametrize("vad_only,tolerance", [(False, 0), (False, 3), (True, 1)])
def test_bounded_variants_preserve_v1_canonical_mapping_and_hash(
    max_variants: int,
    vad_only: bool,
    tolerance: int,
):
    *inputs, policy = _case(vad_only=vad_only)
    policy = replace(policy, av_sync_tolerance_audio_tick=tolerance)
    legacy = compile_candidate_av_span(*inputs, policy)
    variants = compile_candidate_av_span_variants(
        *inputs,
        policy,
        max_variants=max_variants,
    )
    assert variants[0].to_mapping() == legacy.to_mapping()
    assert variants[0].canonical_hash == legacy.canonical_hash
    assert len(variants) == min(max_variants, legacy.feasible_count)
    assert all(type(item) is CandidateExactSpanResult for item in variants)


def test_top_three_are_the_canonical_ordered_prefix_of_the_full_oracle():
    request, root, candidate, plan, profile, clock, policy = _case()
    variants = compile_candidate_av_span_variants(
        request,
        root,
        candidate,
        plan,
        profile,
        clock,
        policy,
        max_variants=3,
    )
    relation, _, _ = _oracle(request, root, policy)
    expected = sorted(relation, key=lambda row: row["decision_key"])[:3]
    assert [list(item.canonical_decision_key) for item in variants] == [
        row["decision_key"] for row in expected
    ]
    assert [
        [
            item.video_range.start_pts,
            item.video_range.end_pts,
            item.audio_range.start_pts,
            item.audio_range.end_pts,
        ]
        for item in variants
    ] == [row["endpoints"] for row in expected]
    assert len({item.feasible_relation_sha256 for item in variants}) == 1
    assert len({item.candidate_domain_sha256 for item in variants}) == 1


def test_variant_entry_records_omission_without_claiming_a_materialized_relation():
    value = _variant_set(max_variants=3)
    entry = value.entries[0]
    assert len(entry.variants) == 3
    assert entry.omitted_count == entry.feasible_count - 3
    assert entry.omitted_count > 0
    assert "relation" not in entry.to_mapping()
    assert entry.variants[0].exact_span_result.canonical_hash == compile_candidate_av_span(
        *_case()
    ).canonical_hash


def test_nonbinding_limit_has_zero_omissions():
    value = _variant_set(max_variants=16, narrow=True)
    entry = value.entries[0]
    assert len(entry.variants) == entry.feasible_count
    assert entry.omitted_count == 0


def test_closed_codec_round_trips_exact_bytes_and_value():
    value = _variant_set()
    raw = encode_span_variant_set_json(value)
    decoded = decode_span_variant_set_json(raw, max_bytes=len(raw))
    assert decoded == value
    assert decoded.canonical_hash == value.canonical_hash
    assert json.loads(raw)["schema_version"] == "span-variant-set-v1"


@pytest.mark.parametrize("mutation", ["unknown", "duplicate_entry", "tampered_relation", "tampered_id"])
def test_closed_codec_rejects_unknown_duplicate_and_tampered_values(mutation: str):
    mapping = deepcopy(_variant_set().to_mapping())
    if mutation == "unknown":
        mapping["unexpected"] = True
    elif mutation == "duplicate_entry":
        mapping["entries"].append(deepcopy(mapping["entries"][0]))
    elif mutation == "tampered_relation":
        mapping["entries"][0]["feasible_relation_sha256"] = canonical_sha256(
            {"tampered": "relation"}
        )
    else:
        mapping["entries"][0]["variants"][0]["variant_id"] = canonical_sha256(
            {"tampered": "variant"}
        )
    with pytest.raises(SpanVariantSetError):
        decode_span_variant_set(mapping)


def test_json_codec_rejects_duplicate_keys_before_mapping_decode():
    raw = encode_span_variant_set_json(_variant_set())
    duplicated = b'{"schema_version":"span-variant-set-v1",' + raw[1:]
    with pytest.raises(SpanVariantSetError, match="duplicate JSON key"):
        decode_span_variant_set_json(duplicated, max_bytes=len(duplicated))


@pytest.mark.parametrize("max_variants", [0, 17, True])
def test_variant_limit_is_explicit_and_portably_bounded(max_variants):
    args = _case()
    with pytest.raises((SpanVariantSetError, ExactSpanValidationError)):
        SpanVariantSetPolicy(max_variants)
    with pytest.raises((ExactSpanValidationError, ValueError)):
        compile_candidate_av_span_variants(*args, max_variants=max_variants)


def test_codec_rejects_nonportable_feasible_count():
    mapping = deepcopy(_variant_set().to_mapping())
    mapping["entries"][0]["feasible_count"] = 2**53
    with pytest.raises(SpanVariantSetError, match="exact integer bounds"):
        decode_span_variant_set(mapping)


@pytest.mark.parametrize("limits", [(1, 10_000), (10_000, 1), (10_000, 16)])
def test_variant_search_work_exhaustion_never_returns_a_partial_prefix(limits):
    *inputs, policy = _case()
    policy = replace(
        policy,
        max_video_pair_visits=limits[0],
        max_av_pair_visits=limits[1],
    )
    with pytest.raises(CandidatePairLimitError):
        compile_candidate_av_span_variants(*inputs, policy, max_variants=3)
