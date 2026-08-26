"""Remote-test authority setup using real Store/validator paths and synthetic raw.

The observations and gold anchors are explicitly synthetic, not model or corpus
acceptance. No accepted proof/anchor is fabricated: the independent validator
must create the four-member accepted record in the test PostgreSQL database.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.media import (
    CalibrationAnchor,
    CalibrationProducer,
    ShadowCalibrationAudioClock,
    ShadowCalibrationInvocation,
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationRawBlob,
    ShadowCalibrationRawContext,
    ShadowCalibrationRequestMapping,
    ShadowCalibrationSourceByteLimits,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.shadow_calibration_raw import (
    SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
    SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
    derive_shadow_calibration_raw_response,
    shadow_calibration_anchor_reference_sha256,
)
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline.measure_shadow_calibration_command import (
    MeasureShadowCalibrationCommand,
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationInputs,
    ShadowCalibrationPortResult,
)
from autocut_kernel.pipeline.validate_calibration_record_command import (
    CalibrationValidationLimits,
    ValidateCalibrationRecordCommand,
)
from autocut_kernel.registry import installed_bootstrap
from autocut_kernel.registry.authority_profiles import ShadowCalibrationProfileSource
from autocut_kernel.registry.installed_local_run import decode_local_run_resource
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.store import CalibrationValidationBinding, PostgresRuntimeStore

from auto_cut_bot.pipeline.media_preflight.installed_policy import validate_installed_media_policy
from auto_cut_bot.pipeline.media_preflight.models import LocalMediaPreflightPolicy
from auto_cut_bot.pipeline.media_preflight.shadow_calibration_service_profile import (
    build_funasr_shadow_service_profile,
)
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource


def calibration_seed_values(policy: LocalMediaPreflightPolicy):
    """Author closed source content/raw contexts before either Git source lock."""
    from tests.authority.test_authority_profile_sources import _shadow_mapping
    from tests.pipeline.test_measure_shadow_calibration_command import _context

    narrative = synthetic_installed_resource().narrative.to_mapping()
    shadow: dict[str, Any] = _shadow_mapping(narrative)
    native = shadow["native_timed_speech"]
    native.update({
        "service_sha256": policy.timed_speech_service_sha256,
        "funasr_version": policy.funasr_version,
        "torch_version": policy.torch_version,
        "max_request_bytes": 8 * 1024 * 1024,
    })
    for producer, kind in zip(native["producers"], ("asr", "vad"), strict=True):
        calibration = policy.calibration(kind)
        producer.update({
            "producer_id": calibration.producer_id,
            "producer_version": calibration.producer_version,
            "generation_policy_sha256": calibration.generation_policy_sha256,
            "detector_sha256": calibration.detector_sha256,
            "calibration_policy_sha256": calibration.calibration_policy_sha256,
            "model_id": getattr(policy, f"{kind}_model_id"),
            "model_revision": getattr(policy, f"{kind}_model_revision"),
            "model_sha256": getattr(policy, f"{kind}_model_sha256"),
            "service_sha256": policy.timed_speech_service_sha256,
        })
    timing = shadow["timing_policies"]
    timing.update({
        "timed_speech_policy_sha256": policy.timed_speech_policy_sha256,
        "word_gap_ms": policy.utterance_gap_milliseconds,
        "vad_merge_gap_ms": policy.vad_merge_gap_milliseconds,
    })
    service = {
        "schema_version": "funasr-shadow-calibration-profile-v1", **native,
        "timed_speech_policy_sha256": timing["timed_speech_policy_sha256"],
        "word_gap_policy_sha256": timing["word_gap_policy_sha256"],
        "vad_merge_policy_sha256": timing["vad_merge_policy_sha256"],
        "utterance_gap_milliseconds": timing["word_gap_ms"],
        "vad_merge_gap_milliseconds": timing["vad_merge_gap_ms"],
    }
    del service["native_port_identity_sha256"]
    native["native_port_identity_sha256"] = canonical_sha256(service)
    clock = ShadowCalibrationAudioClock("audio-stream-1", TimeBase(1, 48_000), 0, 48_000)
    shadow["source_clock_policy"].update({
        "clock_id": clock.clock_id, "time_base": {"numerator": 1, "denominator": 48_000},
    })
    identities = tuple(ShadowCalibrationProducerIdentity(
        CalibrationProducer(item["producer_kind"]),
        **{key: value for key, value in item.items() if key != "producer_kind"},
    ) for item in native["producers"])
    policies = ShadowCalibrationPolicies(
        timing["timed_speech_policy_sha256"], timing["word_gap_policy_sha256"],
        timing["vad_merge_policy_sha256"], timing["word_gap_ms"], timing["vad_merge_gap_ms"],
    )
    # Reuse only a pure raw-context template, never a fake Store or accepted proof.
    original = _context()
    contexts: list[ShadowCalibrationRawContext] = []
    members: list[dict[str, object]] = []
    for ordinal in range(2):
        source_hash = canonical_sha256({"synthetic_source": ordinal})
        source = replace(
            original.source, source_id=f"synthetic-gold-source-{ordinal}",
            source_sha256=source_hash, blob_sha256=source_hash, blob_id=str(uuid4()),
            corpus_member_reference_sha256=canonical_sha256({"synthetic_member": ordinal}),
        )
        # Observed ASR [100,400]ms and VAD [80,600]ms; gold is exactly 100ms
        # later. Independent alignment derives 4800 ticks, exactly 100000us.
        context = replace(
            original, source=source, audio_clock=clock, policies=policies,
            source_byte_limits=ShadowCalibrationSourceByteLimits(
                8 * 1024 * 1024, 8 * 1024 * 1024, 8 * 1024 * 1024,
            ),
            native_profile_identity_sha256=native["native_port_identity_sha256"],
            asr_identity=identities[0], vad_identity=identities[1],
            asr_anchors=(CalibrationAnchor(
                f"asr-gold-{ordinal}", CalibrationProducer.ASR, identities[0].producer_id,
                clock.clock_id, clock.time_base, TickRange(200 * 48, 500 * 48),
            ),),
            vad_anchors=(CalibrationAnchor(
                f"vad-gold-{ordinal}", CalibrationProducer.VAD, identities[1].producer_id,
                clock.clock_id, clock.time_base, TickRange(180 * 48, 700 * 48),
            ),),
        )
        contexts.append(context)
        source = context.source
        members.append({
            "member_id": f"synthetic-raw-{ordinal}",
            "corpus_member_reference_sha256": source.corpus_member_reference_sha256,
            "source_id": source.source_id, "source_sha256": source.source_sha256,
            "source_blob_reference_sha256": canonical_sha256({
                "object_id": source.blob_id, "content_hash": source.blob_sha256,
                "byte_length": source.blob_byte_length, "media_type": source.blob_media_type,
            }),
            "expected_anchor_reference_sha256": shadow_calibration_anchor_reference_sha256(context),
        })
    shadow["calibration_corpus"] = {"corpus_set_sha256": canonical_sha256(members), "members": members}
    return narrative, shadow, tuple(contexts)


def _member(context: ShadowCalibrationRawContext) -> ShadowCalibrationCorpusMember:
    mapping = ShadowCalibrationRequestMapping(
        context.source, context.source_byte_limits, context.container, context.audio_clock,
        context.audio_clock.full_range, context.native_profile_identity_sha256, 65536,
        context.transcript_capability, context.policies.timed_speech_policy_sha256,
        context.policies.word_gap_policy_sha256, context.policies.vad_merge_policy_sha256,
        context.policies.word_gap_ms, context.policies.vad_merge_gap_ms, context.producer_identities,
    )
    invocation = ShadowCalibrationInvocation(
        context.source.corpus_member_reference_sha256, mapping.sha256, mapping, mapping.sha256,
    )
    return ShadowCalibrationCorpusMember(
        context.source.corpus_member_reference_sha256,
        shadow_calibration_anchor_reference_sha256(context), context, invocation,
    )


def calibration_measurement_request(
    profile: ShadowCalibrationProfileSource, registry_sha256: str,
    contexts: tuple[ShadowCalibrationRawContext, ...],
) -> MeasureShadowCalibrationRequest:
    timing = profile.timing_policies
    return MeasureShadowCalibrationRequest(ShadowCalibrationInputs(
        profile.source_sha256, registry_sha256, profile.calibration_corpus.corpus_set_sha256,
        profile.native_timed_speech.native_port_identity_sha256,
        timing.word_gap_policy_sha256, timing.vad_merge_policy_sha256,
        timing.alignment_policy_sha256, timing.acceptance_policy_sha256,
        contexts[0].asr_identity.producer_id, contexts[0].vad_identity.producer_id,
        contexts[0].audio_clock.clock_id, contexts[0].audio_clock.time_base,
    ), tuple(_member(item) for item in contexts))


class SyntheticCalibrationRawPort:
    """Known raw native-format observations, never an accepted-record producer."""

    def measure(self, request: MeasureShadowCalibrationRequest,
                member: ShadowCalibrationCorpusMember) -> ShadowCalibrationPortResult:
        assert member in request.corpus_members
        context, invocation = member.raw_context, member.native_invocation
        raw = canonical_json_bytes({
            "schema_version": SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
            "request_identity_sha256": invocation.request_identity_sha256,
            "source": context.source.to_response_mapping(),
            "audio_clock": context.audio_clock.to_mapping(),
            "requested_range": {"in_tick": 0, "out_tick": 48_000},
            "timed_speech_policy_sha256": context.policies.timed_speech_policy_sha256,
            "word_gap_policy_sha256": context.policies.word_gap_policy_sha256,
            "vad_merge_policy_sha256": context.policies.vad_merge_policy_sha256,
            "native_profile_identity_sha256": context.native_profile_identity_sha256,
            "producer_identities": [item.to_mapping() for item in context.producer_identities],
            "asr_native_output": [{"text": "a", "words": ["a"], "timestamp": [[100, 400]]}],
            "vad_native_output": [{"value": [[80, 600]]}],
        })
        blob = ShadowCalibrationRawBlob(
            raw, SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE, len(raw), sha256_bytes(raw),
        )
        return ShadowCalibrationPortResult(
            invocation, blob, derive_shadow_calibration_raw_response(blob, invocation, context).projection,
        )


def seed_media_preflight_authority(
    store: PostgresRuntimeStore, root: Path, monkeypatch: pytest.MonkeyPatch,
    base_policy: LocalMediaPreflightPolicy,
) -> tuple[InstalledLocalRunProfileResolver, LocalMediaPreflightPolicy]:
    """Use only real persistence/validation; caller owns/reset its verification DB."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    from authority.local_run_resource import emit_locked_local_run_resource
    from authority.shadow_context import build_locked_shadow_context

    import tests.authority.test_shadow_context as source_author
    from tests.authority.test_local_run_calibration import _project
    from tests.authority.test_local_run_context import _local_sources

    narrative, shadow, contexts = calibration_seed_values(base_policy)

    def accept(run: dict[str, Any], old: Any) -> None:
        locked = build_locked_shadow_context(**old.options)
        profile = locked.profiles.shadow
        assert profile is not None
        build_funasr_shadow_service_profile(
            profile=profile, narrative=locked.profiles.narrative,
            expected_profile_contract_sha256=profile.profile_contract_sha256,
        )
        timing = profile.timing_policies
        request = calibration_measurement_request(profile, locked.compilation.registry_sha256, contexts)
        measured = MeasureShadowCalibrationCommand(store, SyntheticCalibrationRawPort()).execute(request)
        assert measured.state == "succeeded", measured
        assert measured.receipt_id is not None and measured.artifact_set_id is not None
        pair = store.read_committed_artifact_set(
            request.job, command_slot_id=measured.command_slot_id, receipt_id=measured.receipt_id,
            artifact_set_id=measured.artifact_set_id, expected_request_hash=request.request_hash,
            expected_command_name="MeasureShadowCalibrationCommand@2.1.3", expected_execution_kind="deterministic",
        )
        binding = CalibrationValidationBinding(
            profile.profile_version, profile.source_sha256, locked.compilation.registry_sha256,
            pair.references[0], pair.references[1], "remote-media-calibration-validation",
        )
        accepted = ValidateCalibrationRecordCommand(
            store, profile, locked.compilation.registry_sha256, locked.profiles.narrative,
            profile.profile_contract_sha256, CalibrationValidationLimits(65536, 131072),
        ).execute(binding)
        assert accepted.state == "succeeded", accepted
        assert accepted.receipt_id is not None and accepted.artifact_set_id is not None
        record = store.read_committed_artifact_set(
            binding.job, command_slot_id=accepted.command_slot_id, receipt_id=accepted.receipt_id,
            artifact_set_id=accepted.artifact_set_id, expected_request_hash=binding.request_hash,
            expected_command_name="ValidateCalibrationRecord@2.1.3", expected_execution_kind="deterministic",
        )
        anchor = store.read_calibration_record_anchor(
            record.references[0], record.references[3],
            expected_profile_source_sha256=profile.source_sha256,
            expected_registry_snapshot_sha256=locked.compilation.registry_sha256,
        )
        assert anchor.record.asr.accepted_bound_tick == anchor.record.vad.accepted_bound_tick == 4800
        _project(run, anchor)
        guard = run["timed_speech_registry_entry"]["guard_policy"]
        guard["word_gap_tick"] = timing.word_gap_ms * 48
        guard["vad_merge_gap_tick"] = timing.vad_merge_gap_ms * 48

    with monkeypatch.context() as author:
        author.setattr(source_author, "_narrative_mapping", lambda: narrative)
        author.setattr(source_author, "_shadow_mapping", lambda _narrative: shadow)
        sources = _local_sources(root, customize_run=accept)
    raw = emit_locked_local_run_resource(**sources.options, store=store)
    resource = decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw))
    with monkeypatch.context() as installation:
        # Only the test installation boundary changes; real binder/bootstrap run.
        installation.setattr(installed_bootstrap, "load_installed_local_run_resource", lambda: resource)
        outcome = installed_bootstrap.bootstrap_installed_local_run(store)
        assert outcome.state == "succeeded", outcome
    resolver = InstalledLocalRunProfileResolver(resource)
    resolver.resolve(store)
    children = {item.producer_id: item for item in resource.local_run.native_timed_speech.producers}
    policy = replace(
        base_policy, timed_speech_calibration_sha256=resource.local_run.calibration.record_ref.content_hash,
        calibrations=tuple(replace(item, calibration_record_sha256=children[item.producer_id].producer_record_sha256)
                           if item.producer_id in children else item for item in base_policy.calibrations),
    )
    validate_installed_media_policy(resource, policy)
    return resolver, policy
