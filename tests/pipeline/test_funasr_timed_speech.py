from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import platform
import runpy
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from autocut_kernel.media import SpeechSourceOutcome, TimeBase, TranscriptSourceOutcome

from auto_cut_bot.pipeline.media_preflight import (
    FunASRHttpTimedSpeechEvidencePort,
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaSourceError,
    TimedSpeechEvidenceRequest,
    TimedSpeechExpectedProducer,
)
from auto_cut_bot.pipeline.media_preflight.runtime_policy import (
    PcCudaRuntimeTimedSpeechPolicy,
    RuntimeTimedSpeechProducerPolicy,
)
from auto_cut_bot.pipeline.media_preflight.runtime_speech import (
    RuntimeTimedSpeechEvidenceRequest,
)
from auto_cut_bot.pipeline.media_preflight.speech_port import SENSEVOICE_WORD_GUARD_PROFILE

H = "sha256:" + "1" * 64
ZERO_SHA256 = "sha256:" + "0" * 64


def producer(kind: str) -> TimedSpeechExpectedProducer:
    return TimedSpeechExpectedProducer(
        kind,
        "asr" if kind == "asr" else "vad",
        "1",
        H,
        H,
        H,
        H,
        50,
        "SenseVoiceSmall" if kind == "asr" else "fsmn",
        "v1",
        H,
        H,
        "sensevoice-word-timestamp" if kind == "asr" else "fsmn-vad-direct",
    )  # type: ignore[arg-type]


def request(tmp_path: Path) -> TimedSpeechEvidenceRequest:
    p = (tmp_path / "x.mp4").resolve()
    p.write_bytes(b"x")
    return TimedSpeechEvidenceRequest(
        p,
        "s",
        H,
        1_000_000,
        1_000_000,
        1_000_000,
        "a",
        TimeBase(1, 1000),
        100,
        4000,
        100,
        4100,
        "http://127.0.0.1:8765/v1/timed-speech-evidence",
        "funasr",
        "1",
        "1.4.3",
        "2.7.1",
        "cpu",
        "required",
        H,
        H,
        (producer("asr"), producer("vad")),
        30,
        100000,
        700,
        350,
    )


def test_legacy_cpu_timed_speech_request_identity_is_frozen(tmp_path: Path) -> None:
    """The CUDA addition may not change the historical CPU wire bytes."""
    value = request(tmp_path)

    assert value.to_mapping()["schema_version"] == "timed-speech-evidence-request-v1"
    assert value.identity_sha256 == (
        "sha256:d178bd98bc9240a9f0a786f5f942bc9f5a55bb12946bbcb7d134ae1b31c1b3b0"
    )


def namespace(
    monkeypatch: pytest.MonkeyPatch, auto_model: type[object] = object
) -> dict[str, object]:
    f = ModuleType("funasr")
    f.AutoModel = auto_model  # type: ignore[attr-defined]
    t = ModuleType("torch")
    t.__version__ = "test"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", f)
    monkeypatch.setitem(sys.modules, "torch", t)
    return runpy.run_path("deploy/funasr/service.py")


def test_torch_runtime_version_normalizes_torch_version_subclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real CUDA wheels expose ``TorchVersion`` instead of an exact ``str``."""
    ns = namespace(monkeypatch)

    class TorchVersion(str):
        pass

    ns["torch"].__version__ = TorchVersion("2.11.0+cu128")  # type: ignore[attr-defined]

    value = ns["torch_runtime_version"]()

    assert value == "2.11.0+cu128"
    assert type(value) is str


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_main_allows_only_loopback_or_container_bind_hosts(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    ns = namespace(monkeypatch)
    service = object()
    app = object()
    calls: list[tuple[object, str, int]] = []

    monkeypatch.setenv("FUNASR_BIND_HOST", host)
    monkeypatch.setenv("FUNASR_PORT", "18765")
    monkeypatch.setitem(ns["main"].__globals__, "Service", lambda: service)
    monkeypatch.setitem(ns["main"].__globals__, "create_app", lambda value: app)
    monkeypatch.setattr(
        ns["web"], "run_app", lambda value, *, host, port: calls.append((value, host, port))
    )

    ns["main"]()

    assert calls == [(app, host, 18765)]


def test_main_rejects_other_bind_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    ns = namespace(monkeypatch)
    monkeypatch.setenv("FUNASR_BIND_HOST", "192.0.2.1")
    monkeypatch.setitem(ns["main"].__globals__, "Service", lambda: object())
    monkeypatch.setitem(ns["main"].__globals__, "create_app", lambda _value: object())

    with pytest.raises(RuntimeError, match="FUNASR_BIND_HOST"):
        ns["main"]()


class _Module:
    def __init__(self, device: str) -> None:
        self.device = device

    def parameters(self) -> tuple[object, ...]:
        return (SimpleNamespace(device=SimpleNamespace(type=self.device)),)

    def buffers(self) -> tuple[object, ...]:
        return ()


class _AutoModel:
    actual_device = "cpu"

    def __init__(self, **_kwargs: object) -> None:
        self.model = _Module(self.actual_device)
        self.vad_model = _Module(self.actual_device)
        self.vad_kwargs: dict[str, object] = {}

    def generate(self, **_kwargs: object) -> list[dict[str, object]]:
        return [{"text": "hello", "words": ["hello"], "timestamp": [[100, 200]]}]

    def inference(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [{"value": [[50, 250]]}]


class _CudaAutoModel(_AutoModel):
    actual_device = "cuda"


class _BlockingAutoModel(_AutoModel):
    started = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    generate_calls = 0
    counter_lock = threading.Lock()

    def generate(self, **kwargs: object) -> list[dict[str, object]]:
        with self.counter_lock:
            type(self).active += 1
            type(self).generate_calls += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            self.started.set()
            if not self.release.wait(10):
                raise RuntimeError("test inference release timed out")
            return super().generate(**kwargs)
        finally:
            with self.counter_lock:
                type(self).active -= 1


class _CountingAutoModel(_AutoModel):
    constructions = 0

    def __init__(self, **kwargs: object) -> None:
        type(self).constructions += 1
        super().__init__(**kwargs)


class _FailingOnceAutoModel(_AutoModel):
    constructions = 0

    def __init__(self, **kwargs: object) -> None:
        type(self).constructions += 1
        if type(self).constructions == 1:
            raise RuntimeError("test startup failure")
        super().__init__(**kwargs)


def _service_profile(
    ns: dict[str, object], asr_path: Path, vad_path: Path, *, device: str = "cpu"
) -> dict[str, object]:
    lock_path = asr_path.parents[2] / "funasr-service.lock"
    ns["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    ns["Service"].__init__.__globals__["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    tree_hash = ns["tree_hash"]
    service_hash = ns["service_hash"]
    detector_hash = ns["detector_hash"]
    profile: dict[str, object] = {
        "schema_version": "funasr-measured-profile-v1",
        "provider_id": "funasr-http-v1",
        "provider_version": "1.0.0",
        "service_sha256": service_hash(),
        "funasr_version": "test",
        "torch_version": "test",
        "device": device,
        "word_timing_capability": "required",
        "max_request_bytes": 1_000_000,
        "profile_calibration_sha256": H,
        "timed_speech_policy_sha256": H,
        "utterance_gap_milliseconds": 700,
        "vad_merge_gap_milliseconds": 350,
    }
    producers = []
    for kind, model_id, path, inference_kind in (
        ("asr", "SenseVoiceSmall", asr_path, "sensevoice-word-timestamp"),
        ("vad", "fsmn-vad", vad_path, "fsmn-vad-direct"),
    ):
        identity = {
            "producer_kind": kind,
            "producer_id": kind,
            "producer_version": "1",
            "generation_policy_sha256": H,
            "detector_sha256": H,
            "calibration_policy_sha256": H,
            "calibration_record_sha256": H,
            "timing_error_bound_tick": 50,
            "model_id": model_id,
            "model_revision": path.name,
            "model_sha256": tree_hash(path),
            "service_sha256": profile["service_sha256"],
            "inference_kind": inference_kind,
        }
        identity["detector_sha256"] = detector_hash(identity, profile)
        producers.append(identity)
    profile["producers"] = producers
    return profile


def test_configured_runtime_timing_projection_is_closed_and_cpu_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    monkeypatch.setitem(
        ns["normal_runtime_timing_compatibility"].__globals__,
        "cuda_decoder_identity_sha256",
        lambda: H,
    )
    projection = ns["normal_runtime_timing_compatibility"](profile, profile["producers"])
    identity = ns["decode_runtime_measurement_identity"](
        ns["canon"](
            {
                "schema_version": "runtime-measurement-identity-v1",
                "runtime_capability_id": "mac_cpu",
                "timing_compatibility": projection,
            }
        )
    )
    assert identity.runtime_capability_id == "mac_cpu"
    assert identity.timing_compatibility.device.device_class == "cpu"


def _shadow_calibration_profile(
    ns: dict[str, object], asr_path: Path, vad_path: Path
) -> dict[str, object]:
    lock_path = asr_path.parents[2] / "funasr-service.lock"
    ns["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    ns["Service"].__init__.__globals__["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    tree_hash = ns["tree_hash"]
    service_hash = ns["service_hash"]
    detector_hash = ns["detector_hash"]
    profile: dict[str, object] = {
        "schema_version": "funasr-shadow-calibration-profile-v1",
        "provider_id": "funasr-http-v1",
        "provider_version": "1.0.0",
        "service_sha256": service_hash(),
        "funasr_version": "test",
        "torch_version": "test",
        "device": "cpu",
        "word_timing_capability": "required",
        "max_request_bytes": 1_000_000,
        "timed_speech_policy_sha256": H,
        "word_gap_policy_sha256": H,
        "vad_merge_policy_sha256": H,
        "utterance_gap_milliseconds": 700,
        "vad_merge_gap_milliseconds": 350,
    }
    producers = []
    for kind, model_id, path, inference_kind in (
        ("asr", "SenseVoiceSmall", asr_path, "sensevoice-word-timestamp"),
        ("vad", "fsmn-vad", vad_path, "fsmn-vad-direct"),
    ):
        identity = {
            "producer_kind": kind,
            "producer_id": kind,
            "producer_version": "1",
            "generation_policy_sha256": H,
            "detector_sha256": H,
            "calibration_policy_sha256": H,
            "model_id": model_id,
            "model_revision": path.name,
            "model_sha256": tree_hash(path),
            "service_sha256": profile["service_sha256"],
            "inference_kind": inference_kind,
        }
        identity["detector_sha256"] = detector_hash(identity, profile)
        producers.append(identity)
    profile["producers"] = producers
    profile["native_port_identity_sha256"] = ns["sha"](
        ns["canon"]({**profile, "producers": producers})
    )
    return profile


def _cuda_shadow_calibration_profile(
    ns: dict[str, object], asr_path: Path, vad_path: Path
) -> dict[str, object]:
    lock_path = asr_path.parents[2] / "funasr-service.lock"
    ns["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    ns["Service"].__init__.__globals__["CANONICAL_SINGLETON_LOCK_PATH"] = lock_path
    torch = ns["torch"]
    torch.version = SimpleNamespace(cuda="12.8")
    torch.cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_capability=lambda: (8, 9),
    )
    ns["Service"]._load_cuda_shadow_profile.__globals__["cuda_decoder_identity_sha256"] = lambda: H
    profile: dict[str, object] = {
        "schema_version": "funasr-cuda-shadow-calibration-profile-v1",
        "provider_id": "funasr-http-v1",
        "provider_version": "1.0.0",
        "build_audit_sha256": ns["service_hash"](),
        "funasr_version": "test",
        "torch_version": "test",
        "device": {
            "device_class": "cuda",
            "cuda_runtime_version": "12.8",
            "gpu_compute_capability": "8.9",
        },
        "word_timing_capability": "required",
        "max_request_bytes": 1_000_000,
        "timed_speech_policy_sha256": H,
        "word_gap_policy_sha256": H,
        "vad_merge_policy_sha256": H,
        "utterance_gap_milliseconds": 700,
        "vad_merge_gap_milliseconds": 350,
    }
    producers: list[dict[str, object]] = []
    for kind, model_id, path, inference_kind in (
        ("asr", "SenseVoiceSmall", asr_path, "sensevoice-word-timestamp"),
        ("vad", "fsmn-vad", vad_path, "fsmn-vad-direct"),
    ):
        identity: dict[str, object] = {
            "producer_kind": kind,
            "producer_id": kind,
            "producer_version": "1",
            "generation_policy_sha256": H,
            "detector_sha256": H,
            "calibration_policy_sha256": H,
            "model_id": model_id,
            "model_revision": path.name,
            "model_sha256": ns["tree_hash"](path),
            "service_sha256": profile["build_audit_sha256"],
            "build_audit_sha256": profile["build_audit_sha256"],
            "inference_kind": inference_kind,
            "inference_identity_sha256": H,
        }
        identity["inference_identity_sha256"] = ns["cuda_inference_identity"](identity, profile)
        identity["detector_sha256"] = ns["cuda_detector_hash"](identity, profile)
        producers.append(identity)
    profile["producers"] = producers
    profile["native_port_identity_sha256"] = ns["cuda_native_port_identity"](profile, producers)
    compatibility = ns["build_timing_compatibility_profile"](
        {
            "schema_version": "timing-compatibility-profile-v1",
            "timing_engine_compatibility_version": "funasr-cuda-timing-v1",
            "build_audit_sha256": profile["build_audit_sha256"],
            "runtime": {
                "funasr_version": profile["funasr_version"],
                "torch_version": profile["torch_version"],
                "device": ns["cuda_device_identity"](),
            },
            "decode": {
                "decoder_identity_sha256": H,
                "resampling_identity_sha256": ns["cuda_resampling_identity_sha256"](),
                "native_protocol_identity_sha256": profile["native_port_identity_sha256"],
            },
            "policies": {
                "word_timestamp_policy_sha256": profile["timed_speech_policy_sha256"],
                "vad_merge_policy_sha256": profile["vad_merge_policy_sha256"],
            },
            "producers": [
                {
                    key: identity[key]
                    for key in (
                        "producer_kind", "producer_id", "producer_version", "model_id",
                        "model_revision", "model_sha256", "inference_identity_sha256",
                    )
                }
                for identity in producers
            ],
        }
    )
    profile["timing_engine_compatibility_version"] = (
        compatibility.timing_engine_compatibility_version
    )
    profile["timing_compatibility_sha256"] = compatibility.timing_compatibility_sha256
    return profile


def _service_environment(
    monkeypatch: pytest.MonkeyPatch, profile: dict[str, object], asr: Path, vad: Path
) -> None:
    values = {
        "FUNASR_PROFILE_JSON": __import__("json").dumps(profile),
        "FUNASR_ASR_MODEL_PATH": str(asr),
        "FUNASR_VAD_MODEL_PATH": str(vad),
        "FUNASR_MAX_REQUEST_BYTES": "1000000",
        "FUNASR_MAX_RESPONSE_BYTES": "1000000",
        "FUNASR_INFERENCE_TIMEOUT_SECONDS": "30",
        "FUNASR_QUEUE_CAPACITY": "3",
        "FUNASR_SHARED_TOKEN": "secret",
        "FUNASR_REQUIRED_PYTHON_VERSION": platform.python_version(),
        "FUNASR_STARTUP_MIN_AVAILABLE_BYTES": "1",
        "FUNASR_INFERENCE_MIN_AVAILABLE_BYTES": "1",
        "FUNASR_MAX_SWAP_USED_BYTES": "999999999999999",
        "FUNASR_SINGLETON_LOCK_PATH": str(asr.parents[2] / "funasr-service.lock"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _request_for_profile(
    tmp_path: Path, profile: dict[str, object], endpoint_url: str
) -> TimedSpeechEvidenceRequest:
    result = request(tmp_path)
    source_sha256 = "sha256:" + hashlib.sha256(result.source_path.read_bytes()).hexdigest()
    producers = tuple(
        TimedSpeechExpectedProducer(**item)
        for item in profile["producers"]  # type: ignore[union-attr]
    )
    return replace(
        result,
        source_sha256=source_sha256,
        endpoint_url=endpoint_url,
        provider_id=profile["provider_id"],
        provider_version=profile["provider_version"],
        funasr_version=profile["funasr_version"],
        torch_version=profile["torch_version"],
        device=profile["device"],
        expected_producers=producers,
    )  # type: ignore[arg-type]


def _headers(request_value: TimedSpeechEvidenceRequest, token: str = "secret") -> dict[str, str]:
    manifest = json.dumps(
        request_value.to_mapping(), sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-Timed-Speech-Manifest": base64.b64encode(manifest).decode(),
        "X-Timed-Speech-Request-SHA256": request_value.identity_sha256,
    }


def _runtime_cuda_request(
    tmp_path: Path,
    ns: dict[str, object],
    profile: dict[str, object],
    endpoint_url: str,
) -> RuntimeTimedSpeechEvidenceRequest:
    """Build the request only from the measured CUDA profile plus accepted refs.

    The accepted-record digests are intentionally synthetic in this service
    boundary test: PostgreSQL resolves them before this request object exists.
    The server must nevertheless receive their immutable closure, rather than
    accepting a legacy CPU local-run profile.
    """
    source = (tmp_path / "runtime-cuda.mp4").resolve()
    body = b"runtime cuda source"
    source.write_bytes(body)
    identity = ns["decode_runtime_measurement_identity"](
        ns["canon"](
            {
                "schema_version": "runtime-measurement-identity-v1",
                "runtime_capability_id": "pc_cuda",
                "timing_compatibility": profile["timing_compatibility"],
            }
        )
    )
    accepted_record = "sha256:" + "2" * 64
    validation_receipt = "sha256:" + "3" * 64
    producers = tuple(
        RuntimeTimedSpeechProducerPolicy(
            item["producer_kind"],  # type: ignore[arg-type]
            item["producer_id"],  # type: ignore[arg-type]
            item["producer_version"],  # type: ignore[arg-type]
            item["generation_policy_sha256"],  # type: ignore[arg-type]
            item["detector_sha256"],  # type: ignore[arg-type]
            item["calibration_policy_sha256"],  # type: ignore[arg-type]
            item["model_id"],  # type: ignore[arg-type]
            item["model_revision"],  # type: ignore[arg-type]
            item["model_sha256"],  # type: ignore[arg-type]
            item["inference_kind"],  # type: ignore[arg-type]
            item["service_sha256"],  # type: ignore[arg-type]
            "sha256:" + ("4" if item["producer_kind"] == "asr" else "5") * 64,
            50,
        )
        for item in profile["producers"]  # type: ignore[union-attr]
    )
    policy = PcCudaRuntimeTimedSpeechPolicy(
        H,
        "pc_cuda",
        "cuda",
        identity.canonical_sha256,
        identity.timing_compatibility_sha256,
        H,
        profile["build_audit_sha256"],  # type: ignore[arg-type]
        H,
        profile["funasr_version"],  # type: ignore[arg-type]
        profile["torch_version"],  # type: ignore[arg-type]
        H,
        H,
        accepted_record,
        validation_receipt,
        profile["native_port_identity_sha256"],  # type: ignore[arg-type]
        "a",
        TimeBase(1, 1000),
        profile["timed_speech_policy_sha256"],  # type: ignore[arg-type]
        profile["word_gap_policy_sha256"],  # type: ignore[arg-type]
        profile["vad_merge_policy_sha256"],  # type: ignore[arg-type]
        H,
        H,
        endpoint_url,
        profile["provider_id"],  # type: ignore[arg-type]
        profile["provider_version"],  # type: ignore[arg-type]
        30,
        1_000_000,
        700,
        350,
        (producers[0], producers[1]),
    )
    return RuntimeTimedSpeechEvidenceRequest(
        source,
        "runtime-cuda-episode",
        "sha256:" + hashlib.sha256(body).hexdigest(),
        1_000_000,
        1_000_000,
        1_000_000,
        "a",
        TimeBase(1, 1000),
        100,
        4000,
        100,
        4100,
        policy,
    )


def _runtime_headers(
    request_value: RuntimeTimedSpeechEvidenceRequest, token: str = "secret"
) -> dict[str, str]:
    manifest = json.dumps(
        request_value.to_mapping(), sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-Timed-Speech-Manifest": base64.b64encode(manifest).decode(),
        "X-Timed-Speech-Request-SHA256": request_value.identity_sha256,
    }


def _shadow_calibration_manifest(
    tmp_path: Path, profile: dict[str, object]
) -> tuple[dict[str, object], bytes]:
    source = (tmp_path / "shadow.mp4").resolve()
    body = b"shadow calibration source"
    source.write_bytes(body)
    request_value = request(tmp_path)
    manifest = {
        "schema_version": "shadow-calibration-funasr-raw-request-v1",
        "source": {
            "source_id": "calibration-corpus-0001",
            "source_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
        },
        "source_byte_limits": request_value.to_mapping()["source_byte_limits"],
        "container": {"media_type": "video/mp4", "safe_suffix": ".mp4"},
        "audio_clock": request_value.to_mapping()["audio_clock"],
        "requested_range": request_value.to_mapping()["requested_range"],
        "expected_producers": profile["producers"],
        "timed_speech_policy_sha256": profile["timed_speech_policy_sha256"],
        "word_gap_policy_sha256": profile["word_gap_policy_sha256"],
        "vad_merge_policy_sha256": profile["vad_merge_policy_sha256"],
        "native_profile_identity_sha256": profile["native_port_identity_sha256"],
        "response_limits": {"max_response_bytes": 1_000_000},
        "timing_policy": {
            "utterance_gap_milliseconds": 700,
            "vad_merge_gap_milliseconds": 350,
        },
        "transcript_capability": request_value.to_mapping()["transcript_capability"],
    }
    return manifest, body


def _shadow_calibration_headers(
    manifest: dict[str, object], token: str = "secret"
) -> dict[str, str]:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-Shadow-Calibration-Manifest": base64.b64encode(raw).decode(),
        "X-Shadow-Calibration-Request-SHA256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _with_duplicate_schema_version(value: dict[str, object]) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return (
        '{"schema_version":'
        + json.dumps(value["schema_version"], separators=(",", ":"))
        + ","
        + raw[1:]
    ).encode()


class _StaticTransport:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def post(self, *_args: object, **_kwargs: object) -> tuple[int, bytes]:
        return self.status, self.body


def test_closed_request_binds_required_capability_and_policy(tmp_path: Path) -> None:
    m = request(tmp_path).to_mapping()
    assert m["profile"]["word_timing_capability"] == "required"  # type: ignore[index]
    assert m["transcript_capability"]["profile"] == SENSEVOICE_WORD_GUARD_PROFILE  # type: ignore[index]
    assert m["transcript_capability"]["sentence"] == "not_applicable"  # type: ignore[index]
    assert m["timing_policy"] == {
        "utterance_gap_milliseconds": 700,
        "vad_merge_gap_milliseconds": 350,
    }


def test_word_guard_rejects_sentence_only_timing_capability(tmp_path: Path) -> None:
    with pytest.raises(LocalMediaPolicyError, match="requires real word timestamps"):
        replace(request(tmp_path), word_timing_capability="sentence_only")  # type: ignore[arg-type]


def test_endpoint_rejects_userinfo_remotehost(tmp_path: Path) -> None:
    from dataclasses import replace

    with pytest.raises(LocalMediaPolicyError):
        replace(
            request(tmp_path),
            endpoint_url="http://127.0.0.1@remotehost:8765/v1/timed-speech-evidence",
        )


def test_real_words_are_not_interpolated_and_group_by_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tr = namespace(monkeypatch)["transcript"](
        [
            {
                "text": "abc",
                "words": ["a", "b", "c"],
                "timestamp": [[0, 100], [200, 300], [1001, 1100]],
            }
        ],
        {"numerator": 1, "denominator": 1000},
        {"in_tick": 100, "out_tick": 4100},
        True,
        700,
    )
    assert [(w["in_tick"], w["out_tick"]) for w in tr["words"]] == [
        (100, 200),
        (300, 400),
        (1101, 1200),
    ]
    assert len(tr["segments"]) == 2
    assert tr["sentences"] == []
    assert tr["completeness"]["sentence"] == "not_applicable"


def test_misaligned_or_nonmonotonic_words_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fn = namespace(monkeypatch)["transcript"]
    tb = {"numerator": 1, "denominator": 1000}
    rr = {"in_tick": 0, "out_tick": 4000}
    assert (
        fn(
            [{"text": "a", "words": ["a"], "timestamp": []}], tb, rr, True, 700
        )["lexical_outcome"]
        == "indeterminate"
    )
    assert (
        fn(
            [{"text": "ab", "words": ["a", "b"], "timestamp": [[0, 100], [50, 150]]}],
            tb,
            rr,
            True,
            700,
        )["lexical_outcome"]
        == "indeterminate"
    )


def test_explicit_empty_asr_distinguishes_silence_from_vad_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ns = namespace(monkeypatch)
    tr = ns["transcript"](
        [{"key": "silence", "text": "", "timestamp": []}],
        {"numerator": 1, "denominator": 1000},
        {"in_tick": 0, "out_tick": 4000},
        True,
        700,
    )
    assert tr["lexical_outcome"] == "no_lexical_content"
    assert tr["completeness"]["sentence"] == "not_applicable"
    assert ns["vad"](
        [{"value": []}],
        {"numerator": 1, "denominator": 1000},
        {"in_tick": 0, "out_tick": 4000},
        350,
    ) == ("no_speech", [])
    state, segs = ns["vad"](
        [{"value": [[0, 100], [450, 600]]}],
        {"numerator": 1, "denominator": 1000},
        {"in_tick": 0, "out_tick": 4000},
        350,
    )
    assert state == "speech" and len(segs) == 1 and segs[0]["confidence_ppm"] is None


def test_resource_snapshots_parse_linux_and_macos_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ns = namespace(monkeypatch)
    linux = ns["linux_resource_snapshot"](
        "MemTotal: 1000 kB\nMemAvailable: 640 kB\nSwapTotal: 200 kB\nSwapFree: 80 kB\n"
    )
    assert linux.available_bytes == 640 * 1024
    assert linux.swap_total_bytes == 200 * 1024
    assert linux.swap_used_bytes == 120 * 1024

    macos = ns["macos_resource_snapshot"](
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free: 10.\nPages inactive: 20.\nPages speculative: 2.\n",
        "total = 4.00G  used = 1.50G  free = 2.50G",
    )
    assert macos.available_bytes == 32 * 16384
    assert macos.swap_total_bytes == 4 * 1024**3
    assert macos.swap_used_bytes == int(1.5 * 1024**3)


@pytest.mark.asyncio
async def test_startup_resource_pressure_fails_before_model_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CountingAutoModel.constructions = 0
    ns = namespace(monkeypatch, _CountingAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    monkeypatch.setenv("FUNASR_STARTUP_MIN_AVAILABLE_BYTES", "1000")
    snapshot = ns["ResourceSnapshot"](999, 100, 0)

    with pytest.raises(RuntimeError, match="resource-pressure"):
        await ns["Service"](resource_reader=lambda: snapshot).load()

    assert _CountingAutoModel.constructions == 0


@pytest.mark.asyncio
async def test_startup_swap_pressure_fails_before_model_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CountingAutoModel.constructions = 0
    ns = namespace(monkeypatch, _CountingAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    monkeypatch.setenv("FUNASR_MAX_SWAP_USED_BYTES", "100")
    snapshot = ns["ResourceSnapshot"](10_000, 1_000, 101)

    with pytest.raises(RuntimeError, match="resource-pressure"):
        await ns["Service"](resource_reader=lambda: snapshot).load()

    assert _CountingAutoModel.constructions == 0

    monkeypatch.setenv("FUNASR_MAX_SWAP_USED_BYTES", "0")
    safe = ns["ResourceSnapshot"](10_000, 1_000, 0)
    service = ns["Service"](resource_reader=lambda: safe)
    await service.load()
    assert _CountingAutoModel.constructions == 1
    await service.close()


def test_singleton_alternate_path_cannot_bypass_canonical_host_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CountingAutoModel.constructions = 0
    ns = namespace(monkeypatch, _CountingAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    monkeypatch.setenv("FUNASR_SINGLETON_LOCK_PATH", str(tmp_path / "alternate.lock"))

    with pytest.raises(RuntimeError, match="canonical singleton lock path"):
        ns["Service"]()

    assert _CountingAutoModel.constructions == 0


@pytest.mark.asyncio
async def test_singleton_symlink_file_fails_before_model_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CountingAutoModel.constructions = 0
    ns = namespace(monkeypatch, _CountingAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    target = tmp_path / "attacker-controlled.lock"
    target.touch()
    lock_path = ns["CANONICAL_SINGLETON_LOCK_PATH"]
    lock_path.symlink_to(target)

    with pytest.raises(RuntimeError, match="symbolic link"):
        await ns["Service"]().load()

    assert _CountingAutoModel.constructions == 0


@pytest.mark.asyncio
async def test_startup_exception_releases_singleton_for_next_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FailingOnceAutoModel.constructions = 0
    ns = namespace(monkeypatch, _FailingOnceAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)

    with pytest.raises(RuntimeError, match="test startup failure"):
        await ns["Service"]().load()
    second = ns["Service"]()
    await second.load()

    assert _FailingOnceAutoModel.constructions == 2
    await second.close()


@pytest.mark.asyncio
async def test_host_singleton_rejects_second_instance_before_model_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CountingAutoModel.constructions = 0
    ns = namespace(monkeypatch, _CountingAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)

    first = ns["Service"]()
    second = ns["Service"]()
    await first.load()
    with pytest.raises(RuntimeError, match="singleton lock"):
        await second.load()

    assert _CountingAutoModel.constructions == 1
    await first.close()


@pytest.mark.asyncio
async def test_service_load_binds_model_bytes_service_and_actual_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)

    service = ns["Service"]()
    await service.load()

    assert service.ready is True
    assert service.measured_profile["service_sha256"] == ns["service_hash"]()
    assert [item["inference_kind"] for item in service.identities] == [
        "sensevoice-word-timestamp",
        "fsmn-vad-direct",
    ]
    assert service.identities == profile["producers"]
    await service.close()


@pytest.mark.asyncio
async def test_service_rejects_device_fallback_and_model_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad, device="mps")
    _service_environment(monkeypatch, profile, asr, vad)

    with pytest.raises(RuntimeError, match="parameter device mismatch"):
        await ns["Service"]().load()

    profile = _service_profile(ns, asr, vad)
    profile["producers"][0]["model_sha256"] = H  # type: ignore[index]
    _service_environment(monkeypatch, profile, asr, vad)
    with pytest.raises(RuntimeError, match="measured asr identity mismatch"):
        await ns["Service"]().load()

    profile = _service_profile(ns, asr, vad)
    profile["word_timing_capability"] = "sentence_only"
    _service_environment(monkeypatch, profile, asr, vad)
    with pytest.raises(RuntimeError, match="requires real word timestamps"):
        await ns["Service"]().load()


@pytest.mark.asyncio
async def test_real_http_boundary_loads_streams_authenticates_and_strictly_decodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    service = ns["Service"]()
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    try:
        endpoint = str(client.make_url("/v1/timed-speech-evidence"))
        request_value = _request_for_profile(tmp_path, profile, endpoint)
        evidence = await asyncio.to_thread(
            FunASRHttpTimedSpeechEvidencePort(shared_token="secret").produce,
            request_value,
        )

        assert len(evidence.transcript.words) == 1
        assert len(evidence.speech_activity.segments) == 1
        assert [item.inference_kind for item in evidence.producer_identities] == [
            "sensevoice-word-timestamp",
            "fsmn-vad-direct",
        ]
        assert evidence.invocation_trace.service_sha256 == profile["service_sha256"]
        assert evidence.transcript.boundary_touch_left is False
        assert evidence.transcript.boundary_touch_right is False

        response = await asyncio.to_thread(
            httpx.post,
            endpoint,
            headers=_headers(request_value),
            content=request_value.source_path.read_bytes(),
        )
        vad_only = response.json()
        vad_only["transcript"]["lexical_outcome"] = "no_lexical_content"
        vad_only["transcript"]["words"] = []
        vad_only["transcript"]["segments"] = []
        vad_only_evidence = FunASRHttpTimedSpeechEvidencePort(
            transport=_StaticTransport(
                200, json.dumps(vad_only, separators=(",", ":"), sort_keys=True).encode()
            ),
            shared_token="secret",
        ).produce(request_value)
        assert vad_only_evidence.transcript.source_outcome is TranscriptSourceOutcome.NO_LEXICAL_CONTENT
        assert vad_only_evidence.speech_activity.source_outcome is SpeechSourceOutcome.SPEECH_DETECTED
        assert vad_only_evidence.speech_activity.segments

        silence = response.json()
        silence["transcript"]["lexical_outcome"] = "no_lexical_content"
        silence["transcript"]["words"] = []
        silence["transcript"]["segments"] = []
        silence["speech_activity"]["speech_outcome"] = "none_detected"
        silence["speech_activity"]["segments"] = []
        silence_evidence = FunASRHttpTimedSpeechEvidencePort(
            transport=_StaticTransport(
                200, json.dumps(silence, separators=(",", ":"), sort_keys=True).encode()
            ),
            shared_token="secret",
        ).produce(request_value)
        assert silence_evidence.transcript.source_outcome is TranscriptSourceOutcome.NO_SPEECH
        assert silence_evidence.speech_activity.source_outcome is SpeechSourceOutcome.NONE_DETECTED

        unauthorized = await client.post(
            "/v1/timed-speech-evidence",
            data=request_value.source_path.read_bytes(),
            headers=_headers(request_value, "wrong"),
        )
        assert unauthorized.status == 401

        duplicate_manifest_headers = _headers(request_value)
        duplicate_manifest_headers["X-Timed-Speech-Manifest"] = base64.b64encode(
            _with_duplicate_schema_version(request_value.to_mapping())
        ).decode()
        duplicate_manifest = await client.post(
            "/v1/timed-speech-evidence",
            data=request_value.source_path.read_bytes(),
            headers=duplicate_manifest_headers,
        )
        assert duplicate_manifest.status == 400
        assert service.admitted == 0

        invalid_container = request_value.to_mapping()
        invalid_container["container"] = {"media_type": "video/mp4", "safe_suffix": ".mov"}
        invalid_container_raw = json.dumps(
            invalid_container, sort_keys=True, separators=(",", ":")
        ).encode()
        invalid_container_headers = _headers(request_value)
        invalid_container_headers["X-Timed-Speech-Manifest"] = base64.b64encode(
            invalid_container_raw
        ).decode()
        invalid_container_headers["X-Timed-Speech-Request-SHA256"] = ns["sha"](
            invalid_container_raw
        )
        rejected_container = await client.post(
            "/v1/timed-speech-evidence",
            data=request_value.source_path.read_bytes(),
            headers=invalid_container_headers,
        )
        assert rejected_container.status == 400
        assert service.admitted == 0

        malformed = response.json()
        malformed["transcript"]["words"][0]["in_tick"] = True
        raw = json.dumps(malformed, separators=(",", ":"), sort_keys=True).encode()
        with pytest.raises(LocalMediaEvidenceError, match="must be an integer"):
            FunASRHttpTimedSpeechEvidencePort(
                transport=_StaticTransport(200, raw), shared_token="secret"
            ).produce(request_value)

        extra = response.json()
        extra["speech_activity"]["segments"][0]["unexpected"] = 1
        with pytest.raises(LocalMediaEvidenceError, match="schema is not closed"):
            FunASRHttpTimedSpeechEvidencePort(
                transport=_StaticTransport(
                    200, json.dumps(extra, separators=(",", ":"), sort_keys=True).encode()
                ),
                shared_token="secret",
            ).produce(request_value)

        clock_drift = response.json()
        clock_drift["timing_error_bounds"]["asr"]["time_base"]["denominator"] += 1
        with pytest.raises(LocalMediaEvidenceError, match="invalid timing error bound"):
            FunASRHttpTimedSpeechEvidencePort(
                transport=_StaticTransport(
                    200,
                    json.dumps(clock_drift, separators=(",", ":"), sort_keys=True).encode(),
                ),
                shared_token="secret",
            ).produce(request_value)

        limit_drift = response.json()
        limit_drift["source_byte_limits"]["effective_max_source_bytes"] -= 1
        with pytest.raises(LocalMediaSourceError, match="source-byte limit drift"):
            FunASRHttpTimedSpeechEvidencePort(
                transport=_StaticTransport(
                    200,
                    json.dumps(limit_drift, separators=(",", ":"), sort_keys=True).encode(),
                ),
                shared_token="secret",
            ).produce(request_value)

        nonmonotonic = response.json()
        nonmonotonic["transcript"]["words"][0]["in_tick"] = 300
        nonmonotonic["transcript"]["words"][0]["out_tick"] = 200
        with pytest.raises(LocalMediaEvidenceError, match="timed speech response is malformed"):
            FunASRHttpTimedSpeechEvidencePort(
                transport=_StaticTransport(
                    200,
                    json.dumps(nonmonotonic, separators=(",", ":"), sort_keys=True).encode(),
                ),
                shared_token="secret",
            ).produce(request_value)

        legacy = request_value.to_mapping()
        legacy["transcript_capability"]["profile"] = "sensevoice_word_utterance_v1"  # type: ignore[index]
        legacy_raw = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        legacy_headers = _headers(request_value)
        legacy_headers["X-Timed-Speech-Manifest"] = base64.b64encode(legacy_raw).decode()
        legacy_headers["X-Timed-Speech-Request-SHA256"] = ns["sha"](legacy_raw)
        legacy_response = await client.post(
            "/v1/timed-speech-evidence",
            data=request_value.source_path.read_bytes(),
            headers=legacy_headers,
        )
        assert legacy_response.status == 409

        limit_manifest = request_value.to_mapping()
        limit_manifest["source_byte_limits"]["service_max_request_bytes"] = 1  # type: ignore[index]
        limit_raw = json.dumps(limit_manifest, sort_keys=True, separators=(",", ":")).encode()
        limit_headers = _headers(request_value)
        limit_headers["X-Timed-Speech-Manifest"] = base64.b64encode(limit_raw).decode()
        limit_headers["X-Timed-Speech-Request-SHA256"] = ns["sha"](limit_raw)
        limit_response = await client.post(
            "/v1/timed-speech-evidence",
            data=request_value.source_path.read_bytes(),
            headers=limit_headers,
        )
        assert limit_response.status == 409
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_shadow_calibration_raw_envelope_closes_over_native_request_without_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _shadow_calibration_profile(ns, asr, vad)
    assert "profile_calibration_sha256" not in profile
    assert all("calibration_record_sha256" not in item for item in profile["producers"])
    assert all("timing_error_bound_tick" not in item for item in profile["producers"])
    _service_environment(monkeypatch, profile, asr, vad)
    service = ns["Service"]()
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    try:
        manifest, body = _shadow_calibration_manifest(tmp_path, profile)
        response = await client.post(
            "/v1/shadow-calibration-funasr-raw",
            data=body,
            headers=_shadow_calibration_headers(manifest),
        )
        assert response.status == 200
        value = await response.json()
        assert set(value) == {
            "schema_version",
            "request_identity_sha256",
            "source",
            "audio_clock",
            "requested_range",
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "native_profile_identity_sha256",
            "producer_identities",
            "asr_native_output",
            "vad_native_output",
        }
        assert value["schema_version"] == "shadow-calibration-funasr-raw-response-v1"
        assert value["request_identity_sha256"] == ns["sha"](ns["canon"](manifest))
        assert value["source"] == manifest["source"]
        assert value["audio_clock"] == manifest["audio_clock"]
        assert value["requested_range"] == manifest["requested_range"]
        assert value["producer_identities"] == profile["producers"]
        assert value["asr_native_output"] == [
            {"text": "hello", "words": ["hello"], "timestamp": [[100, 200]]}
        ]
        assert value["vad_native_output"] == [{"value": [[50, 250]]}]
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        assert "calibration_record_sha256" not in encoded
        assert "profile_calibration_sha256" not in encoded
        assert "timing_error_bound_tick" not in encoded

        duplicate_headers = _shadow_calibration_headers(manifest)
        duplicate_headers["X-Shadow-Calibration-Manifest"] = base64.b64encode(
            _with_duplicate_schema_version(manifest)
        ).decode()
        duplicate_manifest = await client.post(
            "/v1/shadow-calibration-funasr-raw",
            data=body,
            headers=duplicate_headers,
        )
        assert duplicate_manifest.status == 400
        assert service.admitted == 0

        normal_request = request(tmp_path)
        denied_normal = await client.post(
            "/v1/timed-speech-evidence",
            data=normal_request.source_path.read_bytes(),
            headers=_headers(normal_request),
        )
        assert denied_normal.status == 409
        assert service.admitted == 0

        extra = dict(manifest)
        extra["profile_calibration_sha256"] = H
        rejected_extra = await client.post(
            "/v1/shadow-calibration-funasr-raw",
            data=body,
            headers=_shadow_calibration_headers(extra),
        )
        assert rejected_extra.status == 400

        partial = dict(manifest)
        partial["requested_range"] = {"in_tick": 101, "out_tick": 4100}
        rejected_partial = await client.post(
            "/v1/shadow-calibration-funasr-raw",
            data=body,
            headers=_shadow_calibration_headers(partial),
        )
        assert rejected_partial.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cuda_shadow_profile_is_raw_endpoint_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _CudaAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _cuda_shadow_calibration_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    service = ns["Service"]()
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    try:
        assert service.measured_profile["schema_version"] == profile["schema_version"]
        assert service.measured_profile["build_audit_sha256"] == ns["service_hash"]()
        assert (
            service.measured_profile["timing_compatibility"]["timing_compatibility_sha256"]
            == profile["timing_compatibility_sha256"]
        )
        assert all(
            set(identity)
            == {
                "producer_kind", "producer_id", "producer_version", "generation_policy_sha256",
                "detector_sha256", "calibration_policy_sha256", "model_id", "model_revision",
                "model_sha256", "service_sha256", "inference_kind",
            }
            for identity in service.identities
        )

        normal_request = request(tmp_path)
        normal = await client.post(
            "/v1/timed-speech-evidence",
            data=normal_request.source_path.read_bytes(),
            headers=_headers(normal_request),
        )
        assert normal.status == 409
        local_window = await client.post(
            "/v2/timed-speech-window",
            headers={"Authorization": "Bearer secret"},
        )
        assert local_window.status == 409

        manifest, body = _shadow_calibration_manifest(tmp_path, profile)
        manifest["expected_producers"] = service.identities
        raw = await client.post(
            "/v1/shadow-calibration-funasr-raw",
            data=body,
            headers=_shadow_calibration_headers(manifest),
        )
        assert raw.status == 200
        assert (await raw.json())["producer_identities"] == service.identities
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_accepted_cuda_projection_uses_v2_route_and_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _CudaAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _cuda_shadow_calibration_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    service = ns["Service"]()
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    try:
        legacy_endpoint = str(client.make_url("/v1/timed-speech-evidence"))
        request_value = _runtime_cuda_request(
            tmp_path, ns, service.measured_profile, legacy_endpoint
        )

        evidence = await asyncio.to_thread(
            FunASRHttpTimedSpeechEvidencePort(shared_token="secret").produce,
            request_value,
        )
        assert evidence.transcript.words
        assert evidence.speech_activity.segments
        assert evidence.invocation_trace.endpoint_url.endswith(
            "/v2/runtime-timed-speech-evidence"
        )
        assert [identity.device for identity in evidence.producer_identities] == ["cuda", "cuda"]

        # The legacy CPU grammar remains rejected by the CUDA shadow service.
        legacy = request(tmp_path)
        rejected_legacy = await client.post(
            "/v1/timed-speech-evidence",
            data=legacy.source_path.read_bytes(),
            headers=_headers(legacy),
        )
        assert rejected_legacy.status == 409

        # A validly hashed request with a different live measurement cannot
        # reach model inference; capability equality is checked first.
        drifted = request_value.to_mapping()
        authority = drifted["runtime_authority"]
        assert isinstance(authority, dict)
        authority["runtime_measurement_identity_sha256"] = "sha256:" + "6" * 64
        raw = json.dumps(drifted, sort_keys=True, separators=(",", ":")).encode()
        rejected_drift = await client.post(
            "/v2/runtime-timed-speech-evidence",
            data=request_value.source_path.read_bytes(),
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/octet-stream",
                "X-Timed-Speech-Manifest": base64.b64encode(raw).decode(),
                "X-Timed-Speech-Request-SHA256": "sha256:"
                + hashlib.sha256(raw).hexdigest(),
            },
        )
        assert rejected_drift.status == 409
        assert service.admitted == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_shadow_mode_self_measures_identity_without_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _CudaAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _cuda_shadow_calibration_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    monkeypatch.setenv("FUNASR_MODE", "shadow")
    monkeypatch.delenv("FUNASR_PROFILE_JSON")
    ns["Service"]._load_cuda_shadow_profile.__globals__["cuda_decoder_identity_sha256"] = (  # type: ignore[attr-defined]
        lambda: H
    )
    service = ns["Service"]()
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    try:
        assert service.mode == "shadow"
        assert service.ready is True
        identity = await client.get(
            "/v1/shadow-calibration/identity", headers={"Authorization": "Bearer secret"}
        )
        assert identity.status == 200
        payload = await identity.json()
        measured = payload["profile"]
        assert measured["schema_version"] == "funasr-cuda-shadow-calibration-profile-v1"
        assert measured["build_audit_sha256"] == ns["service_hash"]()
        assert measured["timing_compatibility_sha256"] == service.measured_profile[
            "timing_compatibility"
        ]["timing_compatibility_sha256"]
        runtime_identity = await client.get(
            "/v1/runtime-measurement-identity", headers={"Authorization": "Bearer secret"}
        )
        assert runtime_identity.status == 200
        runtime_payload = await runtime_identity.json()
        assert runtime_payload["schema_version"] == (
            "funasr-runtime-measurement-identity-response-v1"
        )
        assert runtime_payload["runtime_measurement_identity"]["runtime_capability_id"] == "pc_cuda"
        assert runtime_payload["runtime_measurement_identity"]["timing_compatibility"] == (
            service.measured_profile["timing_compatibility"]
        )
        assert (await client.get("/v1/shadow-calibration/identity")).status == 401
        assert (await client.get("/v1/runtime-measurement-identity")).status == 401
        assert (
            await client.post("/v1/timed-speech-evidence", headers={"Authorization": "Bearer secret"})
        ).status == 409
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_configured_normal_service_exposes_a_fresh_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    from tests.media.test_calibration_record_persistence import _runtime_measurement

    compatibility = _runtime_measurement(capability_id="mac_cpu").timing_compatibility.to_mapping()
    monkeypatch.setitem(
        ns["runtime_measurement_identity"].__globals__,
        "normal_runtime_timing_compatibility",
        lambda _measured, _producers: compatibility,
    )
    service = ns["Service"]()
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    try:
        response = await client.get(
            "/v1/runtime-measurement-identity", headers={"Authorization": "Bearer secret"}
        )
        assert response.status == 200
        assert (await response.json())["runtime_measurement_identity"] == {
            "schema_version": "runtime-measurement-identity-v1",
            "runtime_capability_id": "mac_cpu",
            "timing_compatibility": compatibility,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cuda_shadow_profile_rejects_fallback_and_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")

    profile = _cuda_shadow_calibration_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    with pytest.raises(RuntimeError, match="model parameter device mismatch"):
        await ns["Service"]().load()

    profile = _cuda_shadow_calibration_profile(ns, asr, vad)
    profile["build_audit_sha256"] = H
    _service_environment(monkeypatch, profile, asr, vad)
    with pytest.raises(RuntimeError, match="build audit identity mismatch"):
        await ns["Service"]().load()

    profile = _cuda_shadow_calibration_profile(ns, asr, vad)
    ns["torch"].cuda.is_available = lambda: False
    _service_environment(monkeypatch, profile, asr, vad)
    with pytest.raises(RuntimeError, match="CUDA runtime identity is unavailable"):
        await ns["Service"]().load()


@pytest.mark.asyncio
async def test_shadow_profile_rejects_duplicate_json_zero_hash_and_equal_producer_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")

    duplicate_profile = _shadow_calibration_profile(ns, asr, vad)
    _service_environment(monkeypatch, duplicate_profile, asr, vad)
    monkeypatch.setenv(
        "FUNASR_PROFILE_JSON", _with_duplicate_schema_version(duplicate_profile).decode()
    )
    with pytest.raises(RuntimeError, match="duplicate JSON object keys"):
        ns["Service"]()

    zero_hash_profile = _shadow_calibration_profile(ns, asr, vad)
    zero_hash_profile["producers"][0]["calibration_policy_sha256"] = ZERO_SHA256
    _service_environment(monkeypatch, zero_hash_profile, asr, vad)
    with pytest.raises(RuntimeError, match="shadow identity is invalid"):
        await ns["Service"]().load()

    duplicate_id_profile = _shadow_calibration_profile(ns, asr, vad)
    duplicate_id_profile["producers"][1]["producer_id"] = "asr"
    _service_environment(monkeypatch, duplicate_id_profile, asr, vad)
    with pytest.raises(RuntimeError, match="producer IDs must be distinct"):
        await ns["Service"]().load()

    unsupported_timing_profile = _shadow_calibration_profile(ns, asr, vad)
    unsupported_timing_profile["word_timing_capability"] = "sentence_only"
    _service_environment(monkeypatch, unsupported_timing_profile, asr, vad)
    with pytest.raises(RuntimeError, match="requires real word timestamps"):
        await ns["Service"]().load()


@pytest.mark.asyncio
async def test_shadow_calibration_endpoint_rejects_ordinary_calibrated_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    normal_profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, normal_profile, asr, vad)
    service = ns["Service"]()
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    try:
        shadow_profile = _shadow_calibration_profile(ns, asr, vad)
        manifest, body = _shadow_calibration_manifest(tmp_path, shadow_profile)
        response = await client.post(
            "/v1/shadow-calibration-funasr-raw",
            data=body,
            headers=_shadow_calibration_headers(manifest),
        )
        assert response.status == 409
        assert service.admitted == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_request_resource_pressure_rejects_before_admission_or_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    monkeypatch.setenv("FUNASR_INFERENCE_MIN_AVAILABLE_BYTES", "1000")
    snapshots = iter(
        (
            ns["ResourceSnapshot"](10_000, 1_000, 0),
            ns["ResourceSnapshot"](999, 1_000, 0),
        )
    )
    service = ns["Service"](resource_reader=lambda: next(snapshots))
    temporary_directory = ns["tempfile"].TemporaryDirectory
    spool_count = 0

    def counted_temporary_directory(*args: object, **kwargs: object) -> object:
        nonlocal spool_count
        spool_count += 1
        return temporary_directory(*args, **kwargs)

    monkeypatch.setattr(ns["tempfile"], "TemporaryDirectory", counted_temporary_directory)
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    try:
        request_value = _request_for_profile(
            tmp_path, profile, str(client.make_url("/v1/timed-speech-evidence"))
        )
        response = await client.post(
            "/v1/timed-speech-evidence",
            data=request_value.source_path.read_bytes(),
            headers=_headers(request_value),
        )

        assert response.status == 503
        assert await response.text() == "resource-pressure"
        assert response.headers["Retry-After"] == "1"
        assert service.admitted == 0
        assert spool_count == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_three_in_flight_requests_serialize_inference_and_fourth_does_not_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _BlockingAutoModel.started.clear()
    _BlockingAutoModel.release.clear()
    _BlockingAutoModel.active = 0
    _BlockingAutoModel.max_active = 0
    _BlockingAutoModel.generate_calls = 0
    ns = namespace(monkeypatch, _BlockingAutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")  # type: ignore[attr-defined]
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    monkeypatch.setenv("FUNASR_QUEUE_CAPACITY", "3")
    service = ns["Service"]()
    temporary_directory = ns["tempfile"].TemporaryDirectory
    spool_count = 0

    def counted_temporary_directory(*args: object, **kwargs: object) -> object:
        nonlocal spool_count
        spool_count += 1
        return temporary_directory(*args, **kwargs)

    monkeypatch.setattr(ns["tempfile"], "TemporaryDirectory", counted_temporary_directory)
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    request_value = _request_for_profile(
        tmp_path, profile, str(client.make_url("/v1/timed-speech-evidence"))
    )
    body = request_value.source_path.read_bytes()
    headers = _headers(request_value)
    first = asyncio.create_task(
        client.post("/v1/timed-speech-evidence", data=body, headers=headers)
    )
    try:
        assert await asyncio.to_thread(_BlockingAutoModel.started.wait, 3)
        second = asyncio.create_task(
            client.post("/v1/timed-speech-evidence", data=body, headers=headers)
        )
        third = asyncio.create_task(
            client.post("/v1/timed-speech-evidence", data=body, headers=headers)
        )
        for _ in range(300):
            if service.admitted == 3 and spool_count == 3:
                break
            await asyncio.sleep(0.01)
        assert service.admitted == 3 and spool_count == 3
        assert _BlockingAutoModel.generate_calls == 1
        assert _BlockingAutoModel.max_active == 1

        fourth = await client.post("/v1/timed-speech-evidence", data=body, headers=headers)
        assert fourth.status == 503
        assert spool_count == 3
        _BlockingAutoModel.release.set()
        responses = await asyncio.gather(first, second, third)
        assert [response.status for response in responses] == [200, 200, 200]
        assert _BlockingAutoModel.generate_calls == 3
        assert _BlockingAutoModel.max_active == 1
    finally:
        _BlockingAutoModel.release.set()
        await client.close()


@pytest.mark.asyncio
async def test_inference_cancellation_keeps_single_model_lock_until_thread_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    service = ns["Service"]()
    started = threading.Event()
    release = threading.Event()

    def blocking_infer(_path: Path) -> tuple[object, object]:
        started.set()
        release.wait(5)
        return [], []

    service.infer = blocking_infer
    task = asyncio.create_task(service.run_inference(tmp_path / "source.mp4"))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.02)

    assert service.lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service.lock.locked()


@pytest.mark.asyncio
async def test_model_failure_marks_service_fatal_before_instance_can_be_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = namespace(monkeypatch, _AutoModel)
    asr = tmp_path / "asr" / "snapshots" / "master"
    vad = tmp_path / "vad" / "snapshots" / "v2.0.4"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"asr")
    (vad / "model.pt").write_bytes(b"vad")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    service = ns["Service"]()
    service.ready = True
    exits: list[int] = []
    service._fatal_exit = exits.append

    def failed_infer(_path: Path) -> tuple[object, object]:
        raise RuntimeError("model failed")

    service.infer = failed_infer
    with pytest.raises(RuntimeError, match="fatal exit returned"):
        await service.run_inference(tmp_path / "source.mp4")

    assert exits == [71]
    assert service.ready is False
