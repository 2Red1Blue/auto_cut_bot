from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.media import CalibrationAnchor, CalibrationProducer, TickRange, TimeBase
from autocut_kernel.registry.authority_profiles import (
    CalibrationCorpus,
    CalibrationCorpusMember,
    SourceClockPolicy,
    decode_shadow_calibration_profile_source,
)
from autocut_kernel.source_manifest import decode_source_manifest
from autocut_kernel.store import CommittedArtifactMemberReference, Job, RuntimeStoreError
from autocut_kernel.store.models import MaterializationLimits, PersistedWholeSeriesSourceManifest

from auto_cut_bot.pipeline.media_preflight.shadow_calibration_inputs import (
    CommittedCalibrationSourceHandle,
    ShadowCalibrationInputError,
    resolve_shadow_calibration_inputs,
)
from tests.media.test_prepare_timed_media_evidence_command import _request, _Store
from tests.pipeline.test_shadow_calibration_service_profile import Inputs
from tests.pipeline.test_shadow_calibration_service_profile import (
    inputs as service_inputs,  # noqa: F401
)
from tests.pipeline.test_validate_calibration_record_command import _hash


@dataclass
class SourceStore:
    persisted: PersistedWholeSeriesSourceManifest
    calls: int = 0
    failure: Exception | None = None

    def read_whole_series_source_manifest(
        self, job: Job, artifact_set_id: UUID
    ) -> PersistedWholeSeriesSourceManifest:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert job == self.persisted.source_job
        assert artifact_set_id == self.persisted.artifact_set_id
        return self.persisted


def _anchor_document(asr: CalibrationAnchor, vad: CalibrationAnchor) -> dict[str, object]:
    def mapping(anchor: CalibrationAnchor) -> dict[str, object]:
        return {
            "anchor_id": anchor.anchor_id, "clock_id": anchor.clock_id,
            "producer": anchor.producer.value, "producer_id": anchor.producer_id,
            "expected_range": {
                "in_tick": anchor.expected_range.start_pts, "out_tick": anchor.expected_range.end_pts,
            },
            "time_base": {"numerator": anchor.time_base.numerator, "denominator": anchor.time_base.denominator},
        }
    return {"schema_version": "shadow-calibration-anchor-set-v1", "asr_anchors": [mapping(asr)],
            "vad_anchors": [mapping(vad)]}


@pytest.fixture
def options(service_inputs: Inputs) -> dict[str, Any]:  # noqa: F811 - imported pytest fixture
    fixture_store = _Store()
    _request(fixture_store)
    persisted = fixture_store.source_manifest
    assert persisted is not None and persisted.source_job is not None
    episode = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs).episodes[0]
    source, blob = episode.media_probe.source, episode.proxy_blob
    clock = episode.media_probe.audio_sample_boundaries.context
    profile = service_inputs.profile
    asr = CalibrationAnchor("word-a", CalibrationProducer.ASR, profile.native_timed_speech.producers[0].producer_id,
                            clock.clock_id, clock.time_base, TickRange(10, 20))
    vad = CalibrationAnchor("speech-a", CalibrationProducer.VAD, profile.native_timed_speech.producers[1].producer_id,
                            clock.clock_id, clock.time_base, TickRange(5, 25))
    member = CalibrationCorpusMember(
        "episode-a", _hash("corpus-source-a"), source.source_id, source.content_sha256,
        canonical_json_hash(blob.to_mapping()), canonical_json_hash(_anchor_document(asr, vad)),
    )
    corpus = CalibrationCorpus(canonical_json_hash([member.to_mapping()]), (member,))
    profile = replace(profile, calibration_corpus=corpus,
                      source_clock_policy=SourceClockPolicy(_hash("clock"), clock.clock_id, clock.time_base))
    profile = decode_shadow_calibration_profile_source(
        canonical_json_bytes(profile.to_mapping()), narrative=service_inputs.narrative,
        expected_profile_contract_sha256=service_inputs.contract_hash,
    )
    ref = persisted.reference
    handle = CommittedCalibrationSourceHandle(
        member.corpus_member_reference_sha256, persisted.source_job,
        CommittedArtifactMemberReference(persisted.receipt_id, persisted.artifact_set_id, 0, ref.scope,
                                         ref.artifact_type, ref.logical_id, ref.revision, ref.content_hash),
        persisted.command_slot_id, persisted.canonical_hash, (asr,), (vad,),
    )
    return {
        "store": SourceStore(persisted), "profile": profile, "narrative": service_inputs.narrative,
        "expected_profile_contract_sha256": service_inputs.contract_hash,
        "registry_snapshot_sha256": _hash("registry"), "source_handles": (handle,),
        "limits": MaterializationLimits(1_000_000, 1_000_000, 1024, 1_000_000),
        "max_response_bytes": 32768,
    }


def test_resolve_uses_committed_audio_source_and_independent_anchors(options: dict[str, Any]) -> None:
    result = resolve_shadow_calibration_inputs(**options)
    store, handle = options["store"], options["source_handles"][0]
    decoded = decode_source_manifest(store.persisted.payload_json, store.persisted.proxy_blobs)
    episode = decoded.episodes[0]
    clock = episode.media_probe.audio_sample_boundaries.context
    member = result.request.corpus_members[0]
    assert member.raw_context.audio_clock.clock_id == clock.clock_id != episode.manifest.source_clock_id
    assert member.raw_context.audio_clock.origin_tick == clock.origin_tick
    assert member.raw_context.audio_clock.duration_tick == clock.duration_tick
    assert member.raw_context.audio_clock.time_base == clock.time_base
    assert member.raw_context.asr_anchors == handle.asr_anchors
    assert member.raw_context.vad_anchors == handle.vad_anchors
    assert result.source_bindings[0].owner_job == store.persisted.source_job != result.request.job
    assert result.source_bindings[0].source_blob == store.persisted.proxy_blobs[0]
    assert result.source_handles == options["source_handles"]
    assert json.loads(result.service_profile_bytes)["schema_version"] == "funasr-shadow-calibration-profile-v1"
    assert store.calls == 1  # This Store exposes no materialization/provider/write method.
    assert resolve_shadow_calibration_inputs(**options) == result


@pytest.mark.parametrize("change", ["empty", "duplicate", "foreign", "list"])
def test_incomplete_or_substituted_corpus_rejected_before_store(options: dict[str, Any], change: str) -> None:
    handle = options["source_handles"][0]
    options["source_handles"] = {
        "empty": (), "duplicate": (handle, handle), "list": [handle],
        "foreign": (replace(handle, corpus_member_reference_sha256=_hash("foreign")),),
    }[change]
    with pytest.raises(ShadowCalibrationInputError):
        resolve_shadow_calibration_inputs(**options)
    assert options["store"].calls == 0


@pytest.mark.parametrize("field,value", [
    ("registry_snapshot_sha256", "sha256:" + "0" * 64), ("max_response_bytes", 0),
    ("max_response_bytes", True), ("limits", None),
    ("limits", MaterializationLimits(8192, 4096, 512, 8192)),
    ("expected_profile_contract_sha256", _hash("wrong-contract")),
])
def test_bad_deployment_inputs_rejected_before_store(options: dict[str, Any], field: str, value: object) -> None:
    options[field] = value
    with pytest.raises(ValueError):
        resolve_shadow_calibration_inputs(**options)
    assert options["store"].calls == 0


@pytest.mark.parametrize("field", ["receipt", "slot", "provenance", "revision", "content"])
def test_exact_source_handle_rejects_provenance_drift(options: dict[str, Any], field: str) -> None:
    handle = options["source_handles"][0]
    if field == "slot":
        handle = replace(handle, command_slot_id=uuid4())
    elif field == "provenance":
        handle = replace(handle, source_provenance_sha256=_hash("wrong-provenance"))
    else:
        values = {"receipt": {"receipt_id": uuid4()}, "revision": {"revision": 2},
                  "content": {"content_hash": _hash("wrong-content")}}
        handle = replace(handle, manifest_reference=replace(handle.manifest_reference, **values[field]))
    options["source_handles"] = (handle,)
    with pytest.raises(ShadowCalibrationInputError, match="committed provenance"):
        resolve_shadow_calibration_inputs(**options)


@pytest.mark.parametrize("field", ["source_id", "source_sha256", "source_blob_reference_sha256", "expected_anchor_reference_sha256"])
def test_locked_corpus_identity_drift_fails(options: dict[str, Any], field: str) -> None:
    profile = options["profile"]
    member = replace(profile.calibration_corpus.members[0], **{field: "missing-source" if field == "source_id" else _hash("drift")})
    profile = replace(profile, calibration_corpus=CalibrationCorpus(canonical_json_hash([member.to_mapping()]), (member,)))
    options["profile"] = replace(profile, canonical_sha256=canonical_json_hash(profile.to_mapping()))
    with pytest.raises(ShadowCalibrationInputError):
        resolve_shadow_calibration_inputs(**options)


@pytest.mark.parametrize("drift", ["clock", "time-base", "producer", "range", "anchor-hash"])
def test_independent_anchors_cannot_change_clock_role_range_or_hash(options: dict[str, Any], drift: str) -> None:
    handle = options["source_handles"][0]
    changes = {"clock": {"clock_id": "video-stream-0"}, "time-base": {"time_base": TimeBase(1, 90000)},
               "producer": {"producer_id": "foreign-asr"}, "range": {"expected_range": TickRange(0, 1000)},
               "anchor-hash": {"expected_range": TickRange(11, 21)}}
    options["source_handles"] = (replace(handle, asr_anchors=(replace(handle.asr_anchors[0], **changes[drift]),)),)
    with pytest.raises(ValueError):
        resolve_shadow_calibration_inputs(**options)


def test_video_clock_is_not_substituted_for_audio_policy(options: dict[str, Any]) -> None:
    profile = options["profile"]
    profile = replace(profile, source_clock_policy=replace(profile.source_clock_policy, clock_id="video-stream-0"))
    options["profile"] = replace(profile, canonical_sha256=canonical_json_hash(profile.to_mapping()))
    with pytest.raises(ShadowCalibrationInputError, match="audio clock"):
        resolve_shadow_calibration_inputs(**options)


def test_store_unavailability_propagates_without_rewriting_denial(options: dict[str, Any]) -> None:
    failure = RuntimeStoreError("source database unavailable")
    options["store"].failure = failure
    with pytest.raises(RuntimeStoreError) as caught:
        resolve_shadow_calibration_inputs(**options)
    assert caught.value is failure


@pytest.mark.parametrize("field,value", [("asr_anchors", ()), ("vad_anchors", []), ("owner_job", "path"), ("command_slot_id", "slot")])
def test_handle_requires_exact_typed_anchors_and_source_identity(options: dict[str, Any], field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(options["source_handles"][0], **{field: value})
    assert options["store"].calls == 0
