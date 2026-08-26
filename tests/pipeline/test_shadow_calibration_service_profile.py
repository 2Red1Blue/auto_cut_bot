from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.media.timing_compatibility import build_timing_compatibility_profile
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.registry.authority_profiles import (
    CalibrationAcceptance,
    NativeTimedSpeechProducer,
    NativeTimedSpeechProfile,
    ShadowCalibrationProfileSource,
    Stage1NarrativeProfileSource,
    decode_cuda_shadow_calibration_profile_source,
    decode_shadow_calibration_profile_source,
)

from auto_cut_bot.pipeline.media_preflight.shadow_calibration_service_profile import (
    ShadowCalibrationServiceProfileError,
    build_funasr_cuda_shadow_service_profile,
    build_funasr_shadow_service_profile,
)
from tests.pipeline.test_shadow_calibration_envelope_contract import (
    _configure_service,
    _namespace,
    _shadow_profile,
)
from tests.pipeline.test_validate_calibration_record_command import _fixture, _hash


@dataclass(frozen=True)
class Inputs:
    profile: ShadowCalibrationProfileSource
    narrative: Stage1NarrativeProfileSource
    contract_hash: str
    service_profile: dict[str, Any]
    namespace: dict[str, Any]
    asr_path: Path
    vad_path: Path


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Inputs:
    ns = _namespace(monkeypatch)
    monkeypatch.setattr(cast(Any, ns["importlib"]).metadata, "version", lambda _name: "test")
    asr, vad = tmp_path / "asr" / "snapshots" / "main", tmp_path / "vad" / "snapshots" / "v1"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    service_profile = cast(dict[str, Any], _shadow_profile(ns, asr, vad))
    seed, _, _ = _fixture()
    producers = service_profile["producers"]
    native = NativeTimedSpeechProfile(
        service_profile["service_sha256"],
        service_profile["funasr_version"],
        service_profile["torch_version"],
        service_profile["max_request_bytes"],
        service_profile["native_port_identity_sha256"],
        (NativeTimedSpeechProducer(**producers[0]), NativeTimedSpeechProducer(**producers[1])),
    )
    profile = replace(
        seed.profile,
        native_timed_speech=native,
        timing_policies=replace(
            seed.profile.timing_policies,
            timed_speech_policy_sha256=service_profile["timed_speech_policy_sha256"],
            word_gap_policy_sha256=service_profile["word_gap_policy_sha256"],
            vad_merge_policy_sha256=service_profile["vad_merge_policy_sha256"],
            word_gap_ms=service_profile["utterance_gap_milliseconds"],
            vad_merge_gap_ms=service_profile["vad_merge_gap_milliseconds"],
        ),
    )
    profile = decode_shadow_calibration_profile_source(
        canonical_json_bytes(profile.to_mapping()),
        narrative=seed.narrative,
        expected_profile_contract_sha256=seed.expected_shadow_profile_contract_sha256,
    )
    return Inputs(
        profile,
        seed.narrative,
        seed.expected_shadow_profile_contract_sha256,
        service_profile,
        ns,
        asr,
        vad,
    )


def _build(inputs: Inputs, profile: ShadowCalibrationProfileSource | None = None) -> bytes:
    return build_funasr_shadow_service_profile(
        profile=inputs.profile if profile is None else profile,
        narrative=inputs.narrative,
        expected_profile_contract_sha256=inputs.contract_hash,
    )


def _rehash(profile: ShadowCalibrationProfileSource) -> ShadowCalibrationProfileSource:
    return replace(profile, canonical_sha256=canonical_json_hash(profile.to_mapping()))


def _cuda_source(inputs: Inputs) -> dict[str, object]:
    source = json.loads(json.dumps(inputs.profile.to_mapping()))
    source.update(
        schema_version="autocut-cuda-shadow-calibration-profile-v1",
        profile_id="cuda_shadow_calibration",
        profile_state="cuda_shadow_calibration_v1",
        profile_contract_sha256=_hash("pipeline-cuda-shadow-contract"),
    )
    native = source.pop("native_timed_speech")
    assert isinstance(native, dict)
    build_audit_sha256 = _hash("pipeline-current-cuda-build")
    native["build_audit_sha256"] = build_audit_sha256
    native.pop("service_sha256")
    native["device"] = "cuda"
    producers = native["producers"]
    assert isinstance(producers, list)
    compatibility_producers: list[dict[str, object]] = []
    for producer in producers:
        assert isinstance(producer, dict)
        producer["service_sha256"] = build_audit_sha256
        producer["build_audit_sha256"] = build_audit_sha256
        producer["inference_identity_sha256"] = _hash(
            f"pipeline-{producer['producer_kind']}-inference"
        )
        compatibility_producers.append(
            {
                "producer_kind": producer["producer_kind"],
                "producer_id": producer["producer_id"],
                "producer_version": producer["producer_version"],
                "model_id": producer["model_id"],
                "model_revision": producer["model_revision"],
                "model_sha256": producer["model_sha256"],
                "inference_identity_sha256": producer["inference_identity_sha256"],
            }
        )
    policies = source["timing_policies"]
    assert isinstance(policies, dict)
    native_identity = {
        "schema_version": "funasr-cuda-shadow-calibration-profile-v1",
        **native,
        "device": {
            "device_class": "cuda",
            "cuda_runtime_version": "12.8",
            "gpu_compute_capability": "8.9",
        },
        "timed_speech_policy_sha256": policies["timed_speech_policy_sha256"],
        "word_gap_policy_sha256": policies["word_gap_policy_sha256"],
        "vad_merge_policy_sha256": policies["vad_merge_policy_sha256"],
        "utterance_gap_milliseconds": policies["word_gap_ms"],
        "vad_merge_gap_milliseconds": policies["vad_merge_gap_ms"],
    }
    native_identity.pop("native_port_identity_sha256")
    native_identity.pop("build_audit_sha256")
    identity_producers = native_identity["producers"]
    assert isinstance(identity_producers, list)
    native_identity["producers"] = [
        {
            key: value
            for key, value in producer.items()
            if key not in {"service_sha256", "build_audit_sha256"}
        }
        for producer in identity_producers
        if isinstance(producer, dict)
    ]
    native["native_port_identity_sha256"] = canonical_sha256(native_identity)
    source["cuda_timed_speech"] = native
    source["timing_compatibility"] = build_timing_compatibility_profile(
        {
            "schema_version": "timing-compatibility-profile-v1",
            "timing_engine_compatibility_version": "funasr-cuda-timing-v1",
            "build_audit_sha256": build_audit_sha256,
            "runtime": {
                "funasr_version": native["funasr_version"],
                "torch_version": native["torch_version"],
                "device": {
                    "device_class": "cuda",
                    "cuda_runtime_version": "12.8",
                    "gpu_compute_capability": "8.9",
                },
            },
            "decode": {
                "decoder_identity_sha256": _hash("pipeline-cuda-decoder"),
                "resampling_identity_sha256": _hash("pipeline-cuda-resampling"),
                "native_protocol_identity_sha256": native["native_port_identity_sha256"],
            },
            "policies": {
                "word_timestamp_policy_sha256": policies["timed_speech_policy_sha256"],
                "vad_merge_policy_sha256": policies["vad_merge_policy_sha256"],
            },
            "producers": compatibility_producers,
        }
    ).to_mapping()
    return source


def _decode_cuda_source(inputs: Inputs, source: dict[str, object]):
    return decode_cuda_shadow_calibration_profile_source(
        canonical_json_bytes(source),
        narrative=inputs.narrative,
        expected_profile_contract_sha256=_hash("pipeline-cuda-shadow-contract"),
    )


def test_projection_matches_exact_service_codec_and_hash_without_side_effects(
    inputs: Inputs,
) -> None:
    before = dict(os.environ)
    profile_before = inputs.profile.to_mapping()
    raw = _build(inputs)
    assert raw == inputs.namespace["canon"](inputs.service_profile)
    assert raw == _build(inputs)
    assert json.loads(raw) == inputs.service_profile
    payload = json.loads(raw)
    assert (
        canonical_sha256({k: v for k, v in payload.items() if k != "native_port_identity_sha256"})
        == inputs.profile.native_timed_speech.native_port_identity_sha256
    )
    assert set(payload) == {
        "schema_version",
        "provider_id",
        "provider_version",
        "service_sha256",
        "funasr_version",
        "torch_version",
        "device",
        "word_timing_capability",
        "max_request_bytes",
        "native_port_identity_sha256",
        "timed_speech_policy_sha256",
        "word_gap_policy_sha256",
        "vad_merge_policy_sha256",
        "utterance_gap_milliseconds",
        "vad_merge_gap_milliseconds",
        "producers",
    }
    for producer in payload["producers"]:
        assert (
            not {"producer_record_sha256", "calibration_record_sha256", "timing_error_bound_tick"}
            & producer.keys()
        )
    assert inputs.profile.to_mapping() == profile_before and dict(os.environ) == before


def test_service_codec_escapes_unicode_exactly_like_native_identity_hash(inputs: Inputs) -> None:
    service_profile = {**inputs.service_profile, "funasr_version": "版本-测试"}
    native_hash = canonical_sha256(
        {
            key: value
            for key, value in service_profile.items()
            if key != "native_port_identity_sha256"
        }
    )
    service_profile["native_port_identity_sha256"] = native_hash
    changed = _rehash(
        replace(
            inputs.profile,
            native_timed_speech=replace(
                inputs.profile.native_timed_speech,
                funasr_version="版本-测试",
                native_port_identity_sha256=native_hash,
            ),
        )
    )
    raw = _build(inputs, changed)
    assert raw == inputs.namespace["canon"](service_profile)
    assert b"\\u7248" in raw and "版本".encode() not in raw


@pytest.mark.asyncio
async def test_projection_passes_real_service_startup_with_mock_native_models(
    inputs: Inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    projected = json.loads(_build(inputs))
    _configure_service(monkeypatch, projected, inputs.asr_path, inputs.vad_path)
    lock_path = inputs.asr_path.parents[2] / "funasr-shadow.lock"
    inputs.namespace["Service"].__init__.__globals__["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    service = inputs.namespace["Service"]()
    try:
        await service.load()
        assert service.measured_profile == projected
        assert service.identities == projected["producers"]
    finally:
        await service.close()


@pytest.mark.parametrize(
    "field",
    (
        "timed_speech_policy_sha256",
        "word_gap_policy_sha256",
        "vad_merge_policy_sha256",
        "word_gap_ms",
        "vad_merge_gap_ms",
    ),
)
def test_policy_drift_cannot_silently_replace_locked_native_identity(
    inputs: Inputs, field: str
) -> None:
    value = getattr(inputs.profile.timing_policies, field)
    changed = replace(
        inputs.profile,
        timing_policies=replace(
            inputs.profile.timing_policies,
            **{field: value + 1 if type(value) is int else _hash("changed")},
        ),
    )
    changed = _rehash(changed)
    locked = changed.native_timed_speech.native_port_identity_sha256
    with pytest.raises(ShadowCalibrationServiceProfileError, match="locked native identity"):
        _build(inputs, changed)
    assert changed.native_timed_speech.native_port_identity_sha256 == locked


@pytest.mark.parametrize(
    "field,value",
    (
        ("native_port_identity_sha256", _hash("forged-native")),
        ("funasr_version", "changed"),
        ("torch_version", "changed"),
        ("max_request_bytes", 999999),
    ),
)
def test_native_profile_drift_rejects_even_with_recomputed_source_hash(
    inputs: Inputs, field: str, value: object
) -> None:
    changed = _rehash(
        replace(
            inputs.profile,
            native_timed_speech=replace(inputs.profile.native_timed_speech, **{field: value}),
        )
    )
    with pytest.raises(ShadowCalibrationServiceProfileError, match="locked native identity"):
        _build(inputs, changed)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda p: replace(p, capabilities=replace(p.capabilities, runtime_profile_selection=True)),
        lambda p: replace(p, calibration_acceptance=CalibrationAcceptance(999)),
        lambda p: replace(p, timing_policies=replace(p.timing_policies, word_gap_ms=True)),
        lambda p: replace(
            p,
            native_timed_speech=replace(
                p.native_timed_speech, producers=tuple(reversed(p.native_timed_speech.producers))
            ),
        ),
        lambda p: replace(
            p,
            native_timed_speech=replace(
                p.native_timed_speech,
                producers=(
                    replace(
                        p.native_timed_speech.producers[0],
                        producer_record_sha256=_hash("accepted"),
                        timing_error_bound_tick=1,
                    ),
                    p.native_timed_speech.producers[1],
                ),
            ),
        ),
        lambda p: replace(
            p,
            native_timed_speech=replace(
                p.native_timed_speech,
                producers=(
                    replace(p.native_timed_speech.producers[0], timing_error_bound_tick=1),
                    p.native_timed_speech.producers[1],
                ),
            ),
        ),
    ),
)
def test_typed_self_claims_do_not_bypass_closed_shadow_grammar(
    inputs: Inputs,
    mutation: Callable[[ShadowCalibrationProfileSource], ShadowCalibrationProfileSource],
) -> None:
    with pytest.raises(ValueError):
        _build(inputs, _rehash(mutation(inputs.profile)))


def test_stale_canonical_hash_is_rejected(inputs: Inputs) -> None:
    with pytest.raises(ShadowCalibrationServiceProfileError, match="canonical hash"):
        _build(inputs, replace(inputs.profile, canonical_sha256=_hash("stale")))


@pytest.mark.parametrize("field", ("contract", "narrative"))
def test_external_dependencies_must_match(inputs: Inputs, field: str) -> None:
    changed = (
        replace(inputs, contract_hash=_hash("wrong"))
        if field == "contract"
        else replace(
            inputs,
            narrative=replace(
                inputs.narrative,
                reference=replace(inputs.narrative.reference, source_sha256=_hash("wrong")),
            ),
        )
    )
    with pytest.raises(ValueError):
        _build(changed)


def test_plain_profile_mapping_cannot_be_used_as_authority(inputs: Inputs) -> None:
    with pytest.raises(ShadowCalibrationServiceProfileError, match="exact shadow"):
        build_funasr_shadow_service_profile(
            profile=cast(Any, inputs.profile.to_mapping()),
            narrative=inputs.narrative,
            expected_profile_contract_sha256=inputs.contract_hash,
        )


def test_cuda_shadow_projection_emits_audit_and_derived_compatibility_only_for_shadow(
    inputs: Inputs,
) -> None:
    profile = _decode_cuda_source(inputs, _cuda_source(inputs))
    payload = json.loads(
        build_funasr_cuda_shadow_service_profile(
            profile=profile,
            narrative=inputs.narrative,
            expected_profile_contract_sha256=profile.profile_contract_sha256,
        )
    )

    assert payload["schema_version"] == "funasr-cuda-shadow-calibration-profile-v1"
    assert payload["build_audit_sha256"] == profile.cuda_timed_speech.build_audit_sha256
    assert payload["timing_compatibility_sha256"] == (
        profile.timing_compatibility.timing_compatibility_sha256
    )
    assert payload["timing_engine_compatibility_version"] == (
        profile.timing_compatibility.timing_engine_compatibility_version
    )
    assert payload["device"] == {
        "device_class": "cuda",
        "cuda_runtime_version": "12.8",
        "gpu_compute_capability": "8.9",
    }
    assert "service_sha256" not in payload
    assert all(
        producer["service_sha256"] == payload["build_audit_sha256"]
        and producer["build_audit_sha256"] == payload["build_audit_sha256"]
        for producer in payload["producers"]
    )


def test_cuda_projection_audit_only_rebuild_preserves_derived_compatibility(inputs: Inputs) -> None:
    source = _cuda_source(inputs)
    first = _decode_cuda_source(inputs, source)
    changed = json.loads(json.dumps(source))
    native = changed["cuda_timed_speech"]
    compatibility = changed["timing_compatibility"]
    assert isinstance(native, dict) and isinstance(compatibility, dict)
    audit = _hash("pipeline-audit-only-rebuild")
    native["build_audit_sha256"] = audit
    producers = native["producers"]
    assert isinstance(producers, list)
    for producer in producers:
        assert isinstance(producer, dict)
        producer["service_sha256"] = audit
        producer["build_audit_sha256"] = audit
    compatibility["build_audit_sha256"] = audit
    changed["timing_compatibility"] = build_timing_compatibility_profile(
        {key: value for key, value in compatibility.items() if key != "timing_compatibility_sha256"}
    ).to_mapping()
    rebuilt = _decode_cuda_source(inputs, changed)

    first_payload = json.loads(
        build_funasr_cuda_shadow_service_profile(
            profile=first,
            narrative=inputs.narrative,
            expected_profile_contract_sha256=first.profile_contract_sha256,
        )
    )
    rebuilt_payload = json.loads(
        build_funasr_cuda_shadow_service_profile(
            profile=rebuilt,
            narrative=inputs.narrative,
            expected_profile_contract_sha256=rebuilt.profile_contract_sha256,
        )
    )

    assert first_payload["build_audit_sha256"] != rebuilt_payload["build_audit_sha256"]
    assert first_payload["timing_compatibility_sha256"] == rebuilt_payload[
        "timing_compatibility_sha256"
    ]
