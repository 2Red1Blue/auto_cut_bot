"""PC-CUDA timed-media evidence uses a distinct durable command chain."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from autocut_kernel.media.calibration_record import (
    CalibrationRecordProducerIdentity,
    CalibrationRecordRole,
)
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline.prepare_runtime_timed_media_evidence_command import (
    PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND,
    PrepareRuntimeTimedMediaEvidenceCommand,
    PrepareRuntimeTimedMediaEvidenceRequest,
    ProducedRuntimeTimedMediaEvidence,
    RuntimeTimedMediaEvidenceCommandError,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA,
)
from autocut_kernel.registry.installed_runtime import (
    InstalledRuntimeCapabilityResolver,
    InstalledRuntimeTimedSpeechAuthorityResolver,
)
from autocut_kernel.registry.runtime_timed_speech import (
    RuntimeTimedMediaAuthoritySelector,
    RuntimeTimedSpeechProjection,
)

from tests.authority.test_runtime_timed_speech import (
    _clock,
    _policies,
    _policy,
    _runtime_measurement,
)
from tests.media.test_prepare_timed_media_evidence_command import (
    HASH_A,
    HASH_B,
    HASH_C,
    _bundle,
    _Producer,
    _request,
    _Store,
)


def _runtime_projection(request) -> RuntimeTimedSpeechProjection:
    """Make a fixture authority matching the typed ASR/VAD output fixture."""
    context = request.audio_sample_boundaries.context
    common = {
        "producer_version": "1.0.0",
        "generation_policy_sha256": HASH_A,
        "detector_sha256": HASH_B,
        "calibration_policy_sha256": HASH_C,
        "model_revision": "fixture-revision",
        "model_sha256": HASH_A,
        "service_sha256": HASH_B,
    }
    return RuntimeTimedSpeechProjection(
        runtime_capability_id="pc_cuda",
        runtime_measurement_identity_sha256=HASH_A,
        timing_compatibility_sha256=HASH_B,
        build_audit_sha256=HASH_C,
        funasr_version="1.4.1",
        torch_version="2.11.0+cu128",
        profile_source_sha256=HASH_A,
        registry_snapshot_sha256=HASH_B,
        record_sha256=HASH_A,
        validation_receipt_sha256=HASH_B,
        asr_calibration_record_sha256=HASH_C,
        vad_calibration_record_sha256=HASH_A,
        asr_timing_error_bound_tick=1,
        vad_timing_error_bound_tick=1,
        native_port_identity_sha256=HASH_A,
        source_clock_id=context.clock_id,
        source_time_base=context.time_base,
        timed_speech_policy_sha256=HASH_C,
        word_gap_policy_sha256=HASH_A,
        vad_merge_policy_sha256=HASH_B,
        alignment_policy_sha256=HASH_C,
        acceptance_policy_sha256=HASH_A,
        producers=(
            CalibrationRecordProducerIdentity(
                role=CalibrationRecordRole.ASR,
                producer_id="asr-v1",
                model_id="SenseVoiceSmall",
                inference_kind="sensevoice-word-timestamp",
                **common,
            ),
            CalibrationRecordProducerIdentity(
                role=CalibrationRecordRole.VAD,
                producer_id="vad-v1",
                model_id="fsmn-vad",
                inference_kind="fsmn-vad-direct",
                **{**common, "detector_sha256": HASH_C},
            ),
        ),
    )


def _installed_resolver(monkeypatch: pytest.MonkeyPatch, projection: RuntimeTimedSpeechProjection):
    """Use the real production resolver type; only its Store read is patched."""
    measurement = _runtime_measurement()
    resolver = InstalledRuntimeTimedSpeechAuthorityResolver(
        InstalledRuntimeCapabilityResolver(_policy()),
        RuntimeTimedMediaAuthoritySelector(_policy(), _clock(), _policies(measurement)),
        HASH_A,
    )
    calls: list[object] = []

    def _resolve(self, store: object, actual: object) -> RuntimeTimedSpeechProjection:
        assert self is resolver
        assert actual == measurement
        calls.append(store)
        return projection

    monkeypatch.setattr(InstalledRuntimeTimedSpeechAuthorityResolver, "resolve", _resolve)
    return resolver, calls


class _RuntimeProducer:
    def __init__(self, base: _Producer, *, authority_mutator=None) -> None:
        self.base = base
        self.authority_mutator = authority_mutator

    def prepare(self, request, source, projection) -> ProducedRuntimeTimedMediaEvidence:
        authority = _runtime_authority(projection)
        if self.authority_mutator is not None:
            authority = self.authority_mutator(authority)
        evidence = self.base.prepare(request, source)
        provenance = json.loads(evidence.producer_provenance_json)
        provenance["schema_version"] = "runtime-cuda-media-producer-provenance-v2"
        provenance["runtime_timed_speech_authority"] = authority
        evidence = replace(
            evidence,
            producer_policy_sha256=canonical_sha256(authority),
            producer_policy_json=json.dumps(
                authority, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
            producer_provenance_json=json.dumps(
                provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
            producer_provenance_schema=RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA,
        )
        return ProducedRuntimeTimedMediaEvidence(evidence, projection, authority)


def _runtime_authority(projection: RuntimeTimedSpeechProjection) -> dict[str, object]:
    return {
        "schema_version": "pc-cuda-runtime-timed-speech-policy-v1",
        "static_policy_sha256": HASH_A,
        "runtime_capability_id": "pc_cuda",
        "device": "cuda",
        "runtime_measurement_identity_sha256": projection.runtime_measurement_identity_sha256,
        "timing_compatibility_sha256": projection.timing_compatibility_sha256,
        "runtime_projection_compatibility_sha256": projection.compatibility_hash,
        "build_audit_sha256": projection.build_audit_sha256,
        "runtime_projection_sha256": projection.canonical_hash,
        "runtime": {
            "funasr_version": projection.funasr_version,
            "torch_version": projection.torch_version,
        },
        "profile_source_sha256": projection.profile_source_sha256,
        "registry_snapshot_sha256": projection.registry_snapshot_sha256,
        "accepted_record_sha256": projection.record_sha256,
        "validation_receipt_sha256": projection.validation_receipt_sha256,
        "native_port_identity_sha256": projection.native_port_identity_sha256,
        "source_clock": {
            "clock_id": projection.source_clock_id,
            "time_base": {
                "numerator": projection.source_time_base.numerator,
                "denominator": projection.source_time_base.denominator,
            },
        },
        "timing": {
            "timed_speech_policy_sha256": projection.timed_speech_policy_sha256,
            "word_gap_policy_sha256": projection.word_gap_policy_sha256,
            "vad_merge_policy_sha256": projection.vad_merge_policy_sha256,
            "alignment_policy_sha256": projection.alignment_policy_sha256,
            "acceptance_policy_sha256": projection.acceptance_policy_sha256,
            "utterance_gap_milliseconds": 0,
            "vad_merge_gap_milliseconds": 0,
        },
        "operation": {
            "endpoint_url": "http://127.0.0.1:8080/v2/runtime-timed-speech-evidence",
            "provider_id": "fixture",
            "provider_version": "1",
            "timeout_seconds": 1,
            "max_response_bytes": 1,
        },
        "producers": [
            {
                **producer.to_mapping(),
                "calibration_record_sha256": record_sha256,
                "timing_error_bound_tick": bound_tick,
            }
            for producer, record_sha256, bound_tick in zip(
                projection.producers,
                (
                    projection.asr_calibration_record_sha256,
                    projection.vad_calibration_record_sha256,
                ),
                (
                    projection.asr_timing_error_bound_tick,
                    projection.vad_timing_error_bound_tick,
                ),
                strict=True,
            )
        ],
    }


def _runtime_request(store: _Store) -> PrepareRuntimeTimedMediaEvidenceRequest:
    base = replace(_request(store), idempotency_key="runtime-media-preflight:episode:0")
    base = replace(
        base, producer_policy_sha256=canonical_sha256(_runtime_authority(_runtime_projection(base)))
    )
    return PrepareRuntimeTimedMediaEvidenceRequest(base, _runtime_measurement())


def test_cuda_command_commits_runtime_admission_and_never_reuses_cpu_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    request = _runtime_request(store)
    base_producer = _Producer(_bundle())
    projection = _runtime_projection(request.timed_media_request)
    resolver, calls = _installed_resolver(monkeypatch, projection)
    command = PrepareRuntimeTimedMediaEvidenceCommand(
        store, _RuntimeProducer(base_producer), resolver
    )

    first = command.execute(request)
    replay = command.execute(request)

    assert first.outcome.state == "succeeded"
    assert replay.outcome.state == "succeeded"
    assert base_producer.calls == 1
    assert len(calls) == 2
    assert store.claims[0].command_name == PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND
    assert store.claims[0].request_hash == request.request_hash_for(projection)
    assert store.claims[0].request_hash != request.request_hash
    assert {item.artifact_type for item in store.successes[0].artifacts} == {
        "candidate_timed_evidence_index",
        "committed_video_to_audio_clock_map_certificate",
        "presentation_timeline_probe",
        "root_media_evidence_bundle",
        "runtime_timed_speech_capability_admission",
    }
    admission = next(
        json.loads(item.payload_json)
        for item in store.successes[0].artifacts
        if item.artifact_type == "runtime_timed_speech_capability_admission"
    )
    assert admission["runtime_timed_speech_projection_sha256"] == projection.canonical_hash
    assert "registry_member_reference" not in admission


def test_runtime_command_refuses_cpu_prefix_and_cpu_measurement() -> None:
    store = _Store()
    base = _request(store)

    with pytest.raises(RuntimeTimedMediaEvidenceCommandError, match="CUDA-only prefix"):
        PrepareRuntimeTimedMediaEvidenceRequest(base, _runtime_measurement())

    with pytest.raises(RuntimeTimedMediaEvidenceCommandError, match="pc_cuda"):
        PrepareRuntimeTimedMediaEvidenceRequest(
            replace(base, idempotency_key="runtime-media-preflight:episode:0"),
            _runtime_measurement(capability_id="mac_cpu"),
        )


def test_success_commit_transport_ambiguity_is_not_overwritten_by_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CommitRaisesStore(_Store):
        def commit_command_success(self, success):
            super().commit_command_success(success)
            raise ConnectionError("acknowledgement lost after commit")

    store = _CommitRaisesStore()
    request = _runtime_request(store)
    base_producer = _Producer(_bundle())
    resolver, _ = _installed_resolver(monkeypatch, _runtime_projection(request.timed_media_request))
    command = PrepareRuntimeTimedMediaEvidenceCommand(
        store, _RuntimeProducer(base_producer), resolver
    )

    with pytest.raises(ConnectionError, match="acknowledgement lost"):
        command.execute(request)
    replay = command.execute(request)

    assert not store.rejections
    assert replay.outcome.state == "succeeded"
    assert base_producer.calls == 1


def test_runtime_command_requires_the_installed_store_authority_resolver() -> None:
    class _LookalikeResolver:
        def resolve(self, store: object, measurement: object) -> RuntimeTimedSpeechProjection:
            raise AssertionError("must never be called")

    with pytest.raises(RuntimeTimedMediaEvidenceCommandError, match="installed CUDA authority"):
        PrepareRuntimeTimedMediaEvidenceCommand(
            _Store(),
            _RuntimeProducer(_Producer(_bundle())),
            _LookalikeResolver(),  # type: ignore[arg-type]
        )


def test_runtime_command_refuses_a_legacy_v1_operation_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    request = _runtime_request(store)
    projection = _runtime_projection(request.timed_media_request)
    resolver, _ = _installed_resolver(monkeypatch, projection)

    def _legacy_endpoint(authority: dict[str, object]) -> dict[str, object]:
        changed = dict(authority)
        operation = dict(changed["operation"])
        operation["endpoint_url"] = "http://127.0.0.1:8080/v1/timed-speech-evidence"
        changed["operation"] = operation
        return changed

    outcome = PrepareRuntimeTimedMediaEvidenceCommand(
        store, _RuntimeProducer(_Producer(_bundle()), authority_mutator=_legacy_endpoint), resolver
    ).execute(request)

    assert outcome.outcome.state == "denied"
    assert outcome.outcome.failure_code == "RUNTIME_TIMED_MEDIA_EVIDENCE_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", "pc-cuda-runtime-timed-speech-policy-v0"),
        ("static_policy_sha256", HASH_B),
    ),
)
def test_runtime_command_refuses_policy_schema_or_static_authority_drift(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    store = _Store()
    request = _runtime_request(store)
    projection = _runtime_projection(request.timed_media_request)
    resolver, _ = _installed_resolver(monkeypatch, projection)

    def _drift(authority: dict[str, object]) -> dict[str, object]:
        changed = dict(authority)
        changed[field] = value
        return changed

    outcome = PrepareRuntimeTimedMediaEvidenceCommand(
        store, _RuntimeProducer(_Producer(_bundle()), authority_mutator=_drift), resolver
    ).execute(request)

    assert outcome.outcome.state == "denied"
    assert outcome.outcome.failure_code == "RUNTIME_TIMED_MEDIA_EVIDENCE_INVALID"
