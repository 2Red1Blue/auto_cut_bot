from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import platform
import runpy
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from autocut_kernel.media import (
    SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationMeasurementSummary,
    CalibrationObservation,
    CalibrationProducer,
    ProducerCalibrationMeasurement,
    ShadowCalibrationAsrObservation,
    ShadowCalibrationAudioClock,
    ShadowCalibrationContainer,
    ShadowCalibrationInvocation,
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationProjection,
    ShadowCalibrationRawBlob,
    ShadowCalibrationRawContext,
    ShadowCalibrationRawEvidenceError,
    ShadowCalibrationRequestMapping,
    ShadowCalibrationSource,
    ShadowCalibrationSourceByteLimits,
    ShadowCalibrationTranscriptCapability,
    ShadowCalibrationWordGapSegment,
    TickRange,
    TimeBase,
    decode_shadow_calibration_raw_response,
    shadow_calibration_anchor_reference_sha256,
)
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline.measure_shadow_calibration_command import (
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationInputs,
)
from autocut_kernel.store import BlobRef, Job
from autocut_kernel.store.models import MaterializationLimits, VerifiedMaterializedBlob

from auto_cut_bot.pipeline.media_preflight.shadow_calibration_http import (
    ShadowCalibrationHttpMeasurementPort,
    ShadowCalibrationSourceBinding,
)

HASH = "sha256:" + "1" * 64
RESPONSE_LIMIT = 1_000_000
SOURCE_BYTES = b"cross-boundary shadow calibration source"
TIME_BASE = TimeBase(1, 1_000)
CLOCK_ID = "shadow-audio-clock"


class _Module:
    def __init__(self) -> None:
        self.device = "cpu"

    def parameters(self) -> tuple[object, ...]:
        return (SimpleNamespace(device=SimpleNamespace(type=self.device)),)

    def buffers(self) -> tuple[object, ...]:
        return ()


class _AutoModel:
    def __init__(self, **_kwargs: object) -> None:
        self.model = _Module()
        self.vad_model = _Module()
        self.vad_kwargs: dict[str, object] = {}

    def generate(self, **_kwargs: object) -> list[dict[str, object]]:
        return [{"text": "hello", "words": ["hello"], "timestamp": [[100, 200]]}]

    def inference(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [{"value": [[50, 250]]}]


def _namespace(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    funasr = ModuleType("funasr")
    funasr.AutoModel = _AutoModel  # type: ignore[attr-defined]
    torch = ModuleType("torch")
    torch.__version__ = "test"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", funasr)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return runpy.run_path("deploy/funasr/service.py")


def _shadow_profile(ns: dict[str, object], asr: Path, vad: Path) -> dict[str, object]:
    service_hash = cast(Any, ns["service_hash"])
    tree_hash = cast(Any, ns["tree_hash"])
    detector_hash = cast(Any, ns["detector_hash"])
    profile: dict[str, object] = {
        "schema_version": "funasr-shadow-calibration-profile-v1",
        "provider_id": "funasr-http-v1",
        "provider_version": "1.0.0",
        "service_sha256": service_hash(),
        "funasr_version": "test",
        "torch_version": "test",
        "device": "cpu",
        "word_timing_capability": "required",
        "max_request_bytes": RESPONSE_LIMIT,
        "timed_speech_policy_sha256": HASH,
        "word_gap_policy_sha256": HASH,
        "vad_merge_policy_sha256": HASH,
        "utterance_gap_milliseconds": 700,
        "vad_merge_gap_milliseconds": 350,
    }
    producers: list[dict[str, object]] = []
    for kind, model_id, path, inference_kind in (
        ("asr", "SenseVoiceSmall", asr, "sensevoice-word-timestamp"),
        ("vad", "fsmn-vad", vad, "fsmn-vad-direct"),
    ):
        identity: dict[str, object] = {
            "producer_kind": kind,
            "producer_id": f"{kind}-shadow",
            "producer_version": "1",
            "generation_policy_sha256": HASH,
            "detector_sha256": HASH,
            "calibration_policy_sha256": HASH,
            "model_id": model_id,
            "model_revision": path.name,
            "model_sha256": tree_hash(path),
            "service_sha256": profile["service_sha256"],
            "inference_kind": inference_kind,
        }
        identity["detector_sha256"] = detector_hash(identity, profile)
        producers.append(identity)
    profile["producers"] = producers
    profile["native_port_identity_sha256"] = cast(Any, ns["sha"])(
        cast(Any, ns["canon"])({**profile, "producers": producers})
    )
    return profile


def _configure_service(
    monkeypatch: pytest.MonkeyPatch,
    profile: dict[str, object],
    asr: Path,
    vad: Path,
) -> None:
    for key, value in {
        "FUNASR_PROFILE_JSON": json.dumps(profile),
        "FUNASR_ASR_MODEL_PATH": str(asr),
        "FUNASR_VAD_MODEL_PATH": str(vad),
        "FUNASR_MAX_REQUEST_BYTES": str(RESPONSE_LIMIT),
        "FUNASR_MAX_RESPONSE_BYTES": str(RESPONSE_LIMIT),
        "FUNASR_INFERENCE_TIMEOUT_SECONDS": "30",
        "FUNASR_QUEUE_CAPACITY": "3",
        "FUNASR_SHARED_TOKEN": "test-shadow-token",
        "FUNASR_REQUIRED_PYTHON_VERSION": platform.python_version(),
        "FUNASR_STARTUP_MIN_AVAILABLE_BYTES": "1",
        "FUNASR_INFERENCE_MIN_AVAILABLE_BYTES": "1",
        "FUNASR_MAX_SWAP_USED_BYTES": "999999999999999",
        "FUNASR_SINGLETON_LOCK_PATH": str(asr.parents[2] / "funasr-shadow.lock"),
    }.items():
        monkeypatch.setenv(key, value)


def _identity(value: dict[str, object]) -> ShadowCalibrationProducerIdentity:
    return ShadowCalibrationProducerIdentity(
        CalibrationProducer(cast(str, value["producer_kind"])),
        cast(str, value["producer_id"]),
        cast(str, value["producer_version"]),
        cast(str, value["generation_policy_sha256"]),
        cast(str, value["detector_sha256"]),
        cast(str, value["calibration_policy_sha256"]),
        cast(str, value["model_id"]),
        cast(str, value["model_revision"]),
        cast(str, value["model_sha256"]),
        cast(str, value["inference_kind"]),
        cast(str, value["service_sha256"]),
    )


@dataclass(frozen=True)
class _KernelContract:
    invocation: ShadowCalibrationInvocation
    context: ShadowCalibrationRawContext
    projection: ShadowCalibrationProjection

    @property
    def manifest(self) -> dict[str, object]:
        return self.invocation.request_mapping.to_mapping()


def _kernel_contract(profile: dict[str, object]) -> _KernelContract:
    source_sha256 = "sha256:" + hashlib.sha256(SOURCE_BYTES).hexdigest()
    source = ShadowCalibrationSource(
        "cross-boundary-corpus-0001",
        source_sha256,
        HASH,
        str(uuid4()),
        source_sha256,
        len(SOURCE_BYTES),
        "video/mp4",
    )
    limits = ShadowCalibrationSourceByteLimits(RESPONSE_LIMIT, RESPONSE_LIMIT, RESPONSE_LIMIT)
    container = ShadowCalibrationContainer("video/mp4", ".mp4")
    clock = ShadowCalibrationAudioClock(CLOCK_ID, TIME_BASE, 0, 5_000)
    policies = ShadowCalibrationPolicies(HASH, HASH, HASH, 700, 350)
    capability = ShadowCalibrationTranscriptCapability(
        "sensevoice_word_guard_v1",
        "complete",
        "utterance_gap_protected_range",
        "not_applicable",
        "complete",
        "required",
    )
    producer_values = cast(list[dict[str, object]], profile["producers"])
    asr_identity, vad_identity = (_identity(producer_values[0]), _identity(producer_values[1]))
    native_identity = cast(str, profile["native_port_identity_sha256"])
    request = ShadowCalibrationRequestMapping(
        source,
        limits,
        container,
        clock,
        clock.full_range,
        native_identity,
        RESPONSE_LIMIT,
        capability,
        policies.timed_speech_policy_sha256,
        policies.word_gap_policy_sha256,
        policies.vad_merge_policy_sha256,
        policies.word_gap_ms,
        policies.vad_merge_gap_ms,
        (asr_identity, vad_identity),
    )
    invocation = ShadowCalibrationInvocation(HASH, request.sha256, request, request.sha256)
    asr_observation = CalibrationObservation(
        "asr-word-00000000",
        CalibrationProducer.ASR,
        asr_identity.producer_id,
        "sensevoice-word-timestamp",
        CLOCK_ID,
        TIME_BASE,
        TickRange(100, 200),
    )
    vad_observation = CalibrationObservation(
        "vad-segment-00000000",
        CalibrationProducer.VAD,
        vad_identity.producer_id,
        "fsmn-vad-direct",
        CLOCK_ID,
        TIME_BASE,
        TickRange(50, 250),
    )
    asr_anchor = CalibrationAnchor(
        "asr-anchor-00000000",
        CalibrationProducer.ASR,
        asr_identity.producer_id,
        CLOCK_ID,
        TIME_BASE,
        TickRange(101, 200),
    )
    vad_anchor = CalibrationAnchor(
        "vad-anchor-00000000",
        CalibrationProducer.VAD,
        vad_identity.producer_id,
        CLOCK_ID,
        TIME_BASE,
        TickRange(50, 249),
    )
    context = ShadowCalibrationRawContext(
        source,
        limits,
        container,
        clock,
        policies,
        native_identity,
        capability,
        asr_identity,
        vad_identity,
        (asr_anchor,),
        (vad_anchor,),
    )
    asr_measurement = ProducerCalibrationMeasurement(
        CalibrationProducer.ASR,
        asr_identity.producer_id,
        "sensevoice-word-timestamp",
        CLOCK_ID,
        TIME_BASE,
        (CalibrationAnchorMatch(asr_anchor, asr_observation),),
        1,
    )
    vad_measurement = ProducerCalibrationMeasurement(
        CalibrationProducer.VAD,
        vad_identity.producer_id,
        "fsmn-vad-direct",
        CLOCK_ID,
        TIME_BASE,
        (CalibrationAnchorMatch(vad_anchor, vad_observation),),
        1,
    )
    projection = ShadowCalibrationProjection(
        native_identity,
        invocation.request_identity_sha256,
        (ShadowCalibrationAsrObservation(asr_observation, "hello"),),
        (ShadowCalibrationWordGapSegment("asr-segment-00000000", "hello", TickRange(100, 200)),),
        (vad_observation,),
        CalibrationMeasurementSummary(asr_measurement, vad_measurement),
    )
    return _KernelContract(invocation, context, projection)


def _headers(manifest: dict[str, object]) -> dict[str, str]:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {
        "Authorization": "Bearer test-shadow-token",
        "Content-Type": "application/octet-stream",
        "X-Shadow-Calibration-Manifest": base64.b64encode(raw).decode(),
        "X-Shadow-Calibration-Request-SHA256": canonical_sha256(manifest),
    }


@dataclass
class _SourceLease:
    reference: BlobRef
    path: Path
    closed: bool = False

    def close(self) -> None:
        self.path.unlink(missing_ok=True)
        self.closed = True


@dataclass
class _OwnerStore:
    """Fixture materialization only; real Store claim verification is tested separately."""

    owner: Job
    lease: _SourceLease

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> VerifiedMaterializedBlob:
        assert job == self.owner
        assert reference == self.lease.reference
        assert len(SOURCE_BYTES) <= limits.effective_max_source_bytes
        return self.lease


async def _verify_real_adapter(
    contract: _KernelContract, client: TestClient, tmp_path: Path
) -> None:
    context = contract.context
    member = ShadowCalibrationCorpusMember(
        context.source.corpus_member_reference_sha256,
        shadow_calibration_anchor_reference_sha256(context),
        context,
        contract.invocation,
    )
    request = MeasureShadowCalibrationRequest(
        ShadowCalibrationInputs(
            HASH, HASH, HASH, context.native_profile_identity_sha256,
            context.policies.word_gap_policy_sha256, context.policies.vad_merge_policy_sha256,
            HASH, HASH, context.asr_identity.producer_id, context.vad_identity.producer_id,
            CLOCK_ID, TIME_BASE,
        ),
        (member,),
    )
    source = context.source
    reference = BlobRef(
        UUID(source.blob_id), source.blob_sha256, source.blob_byte_length, source.blob_media_type
    )
    owner = Job("original-calibration-corpus-owner", "shadow")
    assert owner != request.job
    source_path = tmp_path / "verified-adapter-source.mp4"
    source_path.write_bytes(SOURCE_BYTES)
    lease = _SourceLease(reference, source_path)
    adapter = ShadowCalibrationHttpMeasurementPort(
        expected_request=request,
        source_bindings=(ShadowCalibrationSourceBinding(member.corpus_member_reference_sha256, owner, reference),),
        store=_OwnerStore(owner, lease),
        limits=MaterializationLimits(RESPONSE_LIMIT, RESPONSE_LIMIT, 1024, RESPONSE_LIMIT),
        endpoint_url=str(client.make_url("/v1/shadow-calibration-funasr-raw")),
        shared_token="test-shadow-token",
        timeout_seconds=3,
        max_response_bytes=RESPONSE_LIMIT,
    )
    result = await asyncio.to_thread(adapter.measure, request, member)
    assert result.invocation == contract.invocation
    assert result.projection == contract.projection
    assert lease.closed and not source_path.exists()


@pytest.mark.asyncio
async def test_shadow_service_response_is_a_kernel_decodable_closed_calibration_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ns = _namespace(monkeypatch)
    monkeypatch.setattr(cast(Any, ns["importlib"]).metadata, "version", lambda _name: "test")
    asr = tmp_path / "asr" / "snapshots" / "main"
    vad = tmp_path / "vad" / "snapshots" / "v1"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _shadow_profile(ns, asr, vad)
    _configure_service(monkeypatch, profile, asr, vad)
    lock_path = asr.parents[2] / "funasr-shadow.lock"
    ns["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    cast(Any, ns["Service"]).__init__.__globals__["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    service = cast(Any, ns["Service"])()
    client = TestClient(TestServer(cast(Any, ns["create_app"])(service)))
    await client.start_server()
    try:
        contract = _kernel_contract(profile)
        response = await client.post(
            "/v1/shadow-calibration-funasr-raw",
            data=SOURCE_BYTES,
            headers=_headers(contract.manifest),
        )
        assert response.status == 200
        raw = await response.read()
        blob = ShadowCalibrationRawBlob(
            raw,
            SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
            len(raw),
            "sha256:" + hashlib.sha256(raw).hexdigest(),
        )

        decoded = decode_shadow_calibration_raw_response(
            blob,
            contract.invocation,
            contract.context,
            contract.projection,
        )

        assert decoded.projection == contract.projection
        assert json.loads(raw)["request_identity_sha256"] == contract.invocation.request_identity_sha256
        await _verify_real_adapter(contract, client, tmp_path)

        drifted = json.loads(raw)
        drifted["request_identity_sha256"] = HASH
        drifted_raw = json.dumps(drifted, sort_keys=True, separators=(",", ":")).encode()
        with pytest.raises(ShadowCalibrationRawEvidenceError):
            decode_shadow_calibration_raw_response(
                ShadowCalibrationRawBlob(
                    drifted_raw,
                    SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
                    len(drifted_raw),
                    "sha256:" + hashlib.sha256(drifted_raw).hexdigest(),
                ),
                contract.invocation,
                contract.context,
                contract.projection,
            )
    finally:
        await client.close()
