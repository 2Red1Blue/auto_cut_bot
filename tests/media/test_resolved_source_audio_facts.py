"""Real Source/VLM resolution over synthetic Store rows, no native/DB claim."""

import json
from dataclasses import replace

import pytest
from autocut_kernel.media.audio_stream_facts import AudioStreamFacts, SelectedAudioStreamMetadata
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    ResolvedPrepareTimedMediaEvidenceRequest,
    resolve_committed_timed_media_request,
)
from autocut_kernel.source_manifest import decode_source_manifest
from autocut_kernel.store.models import canonical_payload_hash

from tests.media.test_prepare_timed_media_evidence_command import (
    _register_semantic_inputs,
    _request,
    _Store,
)


def source_audio_facts_case(*, sample_rate=96000, channels=2):
    """Rebuild full synthetic Source/VLM provenance after adding measured layout."""
    store = _Store()
    request = _request(store)
    persisted = store.source_manifest
    decoded = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs)
    episode = decoded.episodes[0]
    probe, audio = episode.media_probe.presentation_timeline_probe, episode.media_probe.audio_sample_boundaries
    context = audio.context
    metadata = SelectedAudioStreamMetadata(probe.audio.stream_index, context.time_base,
                                           context.origin_tick, sample_rate, channels)
    facts = AudioStreamFacts(context.source_id, context.source_sha256, probe.audio.stream_index,
        context.clock_id, context.time_base, context.origin_tick, context.end_tick, sample_rate, channels,
        audio.canonical_hash, metadata, metadata.canonical_hash, canonical_sha256(probe.probe_execution.to_mapping()))
    decoded = replace(decoded, episodes=(replace(episode, media_probe=replace(episode.media_probe, audio_stream_facts=facts)),))
    raw = json.dumps(decoded.to_mapping(), sort_keys=True, separators=(",", ":"))
    persisted = replace(persisted, reference=replace(persisted.reference, content_hash=canonical_payload_hash(raw)), payload_json=raw)
    store.source_manifest = persisted
    selector, pack = _register_semantic_inputs(store, with_candidates=True)
    request = replace(request, source_manifest_reference=persisted.reference,
                      source_provenance_sha256=persisted.canonical_hash, semantic_inputs_request=selector, semantic_pack=pack)
    return store, request, facts


def test_old_sources_remain_absent_and_old_resolved_payload_unchanged():
    store = _Store()
    request = _request(store)
    result = resolve_committed_timed_media_request(store, request)
    assert result.audio_stream_facts is None
    old = ResolvedPrepareTimedMediaEvidenceRequest(request, result.presentation_timeline_probe)
    assert result.canonical_payload() == old.canonical_payload()
    assert "audio_stream_facts" not in result.canonical_payload()
    assert not store.claims and not store.materializations


@pytest.mark.parametrize("rate,channels", [(96000, 2), (96000, 6), (32000, 1)])
def test_actual_resolver_retains_exact_native_leaf_without_reciprocal_clock_guess(rate, channels):
    store, request, facts = source_audio_facts_case(sample_rate=rate, channels=channels)
    result = resolve_committed_timed_media_request(store, request)
    assert result.audio_stream_facts == facts
    assert facts.sample_rate == rate and facts.channels == channels
    assert facts.sample_rate != facts.time_base.denominator
    # Appending the resolved leaf changes no historical request/root-input bytes.
    without_leaf = ResolvedPrepareTimedMediaEvidenceRequest(request, result.presentation_timeline_probe)
    assert result.canonical_payload() == without_leaf.canonical_payload()
    assert result.root_input_manifest_sha256 == without_leaf.root_input_manifest_sha256
    assert not store.claims and not store.materializations


@pytest.mark.parametrize("field,value", [("probe_execution_sha256", "sha256:" + "f" * 64),
                                        ("audio_sample_boundary_set_sha256", "sha256:" + "e" * 64),
                                        ("source_sha256", "sha256:" + "d" * 64)])
def test_rehashed_wrong_leaf_is_rejected_by_real_source_decode_before_claim(field, value):
    store, request, _facts = source_audio_facts_case()
    persisted = store.source_manifest
    payload = json.loads(persisted.payload_json)
    payload["episodes"][0]["media_probe"]["audio_stream_facts"][field] = value
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    persisted = replace(persisted, reference=replace(persisted.reference, content_hash=canonical_payload_hash(raw)), payload_json=raw)
    store.source_manifest = persisted
    selector = replace(request.semantic_inputs_request,
                       source_manifest=replace(request.semantic_inputs_request.source_manifest, content_hash=persisted.reference.content_hash))
    changed = replace(request, source_manifest_reference=persisted.reference,
                      source_provenance_sha256=persisted.canonical_hash, semantic_inputs_request=selector)
    with pytest.raises(ValueError):
        resolve_committed_timed_media_request(store, changed)
    assert not store.claims and not store.materializations and store.semantic_reads == 0


def test_direct_resolved_leaf_requires_exact_matching_facts():
    store, request, facts = source_audio_facts_case()
    resolved = resolve_committed_timed_media_request(store, request)
    for invalid in ({}, replace(facts, probe_execution_sha256="sha256:" + "f" * 64)):
        with pytest.raises(ValueError):
            replace(resolved, audio_stream_facts=invalid)
