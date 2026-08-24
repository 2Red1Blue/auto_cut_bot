from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import runpy
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from autocut_kernel.media import TimeBase

from auto_cut_bot.pipeline.media_preflight import (
    FunASRHttpTimedSpeechEvidencePort,
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    TimedSpeechEvidenceRequest,
    TimedSpeechExpectedProducer,
)

H = "sha256:" + "1" * 64


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


class _BlockingAutoModel(_AutoModel):
    started = threading.Event()
    release = threading.Event()

    def generate(self, **kwargs: object) -> list[dict[str, object]]:
        self.started.set()
        if not self.release.wait(10):
            raise RuntimeError("test inference release timed out")
        return super().generate(**kwargs)


def _service_profile(
    ns: dict[str, object], asr_path: Path, vad_path: Path, *, device: str = "cpu"
) -> dict[str, object]:
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
        "FUNASR_QUEUE_CAPACITY": "1",
        "FUNASR_SHARED_TOKEN": "secret",
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


class _StaticTransport:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def post(self, *_args: object, **_kwargs: object) -> tuple[int, bytes]:
        return self.status, self.body


def test_closed_request_binds_required_capability_and_policy(tmp_path: Path) -> None:
    m = request(tmp_path).to_mapping()
    assert m["profile"]["word_timing_capability"] == "required"  # type: ignore[index]
    assert m["timing_policy"] == {
        "utterance_gap_milliseconds": 700,
        "vad_merge_gap_milliseconds": 350,
    }


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
        fn([{"text": "a", "words": ["a"], "timestamp": []}], tb, rr, True, 700)["outcome"]
        == "indeterminate"
    )
    assert (
        fn(
            [{"text": "ab", "words": ["a", "b"], "timestamp": [[0, 100], [50, 150]]}],
            tb,
            rr,
            True,
            700,
        )["outcome"]
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
    assert tr["outcome"] == "no_lexical_content"
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

        unauthorized = await client.post(
            "/v1/timed-speech-evidence",
            data=request_value.source_path.read_bytes(),
            headers=_headers(request_value, "wrong"),
        )
        assert unauthorized.status == 401

        response = await asyncio.to_thread(
            httpx.post,
            endpoint,
            headers=_headers(request_value),
            content=request_value.source_path.read_bytes(),
        )
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
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_queue_capacity_rejects_before_third_request_spools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _BlockingAutoModel.started.clear()
    _BlockingAutoModel.release.clear()
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
    monkeypatch.setenv("FUNASR_QUEUE_CAPACITY", "2")
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
    first = asyncio.create_task(client.post("/v1/timed-speech-evidence", data=body, headers=headers))
    try:
        assert await asyncio.to_thread(_BlockingAutoModel.started.wait, 3)
        second = asyncio.create_task(
            client.post("/v1/timed-speech-evidence", data=body, headers=headers)
        )
        for _ in range(300):
            if service.admitted == 2 and spool_count == 2:
                break
            await asyncio.sleep(0.01)
        assert service.admitted == 2 and spool_count == 2

        third = await client.post("/v1/timed-speech-evidence", data=body, headers=headers)
        assert third.status == 503
        assert spool_count == 2
        _BlockingAutoModel.release.set()
        responses = await asyncio.gather(first, second)
        assert [response.status for response in responses] == [200, 200]
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
