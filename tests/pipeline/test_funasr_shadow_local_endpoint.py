"""Synthetic startup/loopback only: no real model, codec or accepted calibration.

The reusable fixture executes the real window/inference call chain with a fake
decode leaf and fake models reading a marker file. It does NOT claim real PCM.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from autocut_kernel.media.local_audio_window import LocalAudioWindowSpec
from autocut_kernel.media.local_speech_window import (
    DecodedLocalPcmReport,
    LocalSpeechWindowPolicy,
    LocalSpeechWindowRequest,
)
from autocut_kernel.media.local_speech_window_busy import decode_local_speech_window_busy_proof
from autocut_kernel.media.local_speech_window_codec import decode_local_speech_window_response
from autocut_kernel.media.local_speech_window_projection import project_local_speech_window
from autocut_kernel.media.shadow_local_service_profile import (
    SHADOW_LOCAL_SERVICE_PROFILE_SCHEMA,
    build_shadow_local_service_profile,
    decode_shadow_local_service_profile,
)
from autocut_kernel.media.types import TickRange, TimeBase, canonical_sha256

from tests.pipeline.test_funasr_timed_speech import (
    _AutoModel,
    _service_environment,
    _service_profile,
    _shadow_calibration_profile,
    namespace,
)
from tests.pipeline.test_funasr_window_endpoint import SOURCE, H, _headers, _report, _sha

ROUTE = "/v2/shadow-calibration-speech-window"
OTHER = "sha256:" + "2" * 64
MARKER = b"synthetic decoder output, not actual WAV PCM"


def _startup_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    model_calls: list[tuple[str, Path]] = []
    constructions: list[dict[str, object]] = []
    decoder_calls: list[str] = []

    class Model(_AutoModel):
        def __init__(self, **kwargs: object) -> None:
            constructions.append(kwargs)
            super().__init__(**kwargs)

        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            path = Path(cast(str, kwargs["input"]))
            assert path.suffix == ".wav" and path.read_bytes() == MARKER
            assert kwargs["output_timestamp"] is True
            model_calls.append(("asr", path))
            return super().generate(**kwargs)

        def inference(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            path = Path(cast(str, args[0]))
            assert path.suffix == ".wav" and path.read_bytes() == MARKER
            model_calls.append(("vad", path))
            return super().inference(*args, **kwargs)

    ns = cast(dict[str, Any], namespace(monkeypatch, Model))
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")
    asr, vad = tmp_path / "asr/snapshots/master", tmp_path / "vad/snapshots/v2"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"synthetic ASR")
    (vad / "model.pt").write_bytes(b"synthetic VAD")
    mapping = _shadow_calibration_profile(ns, asr, vad)
    del mapping["native_port_identity_sha256"]
    mapping["schema_version"] = SHADOW_LOCAL_SERVICE_PROFILE_SCHEMA
    mapping["decoder_identity_sha256"] = H
    profile = build_shadow_local_service_profile(mapping)
    _service_environment(monkeypatch, profile.to_mapping(), asr, vad)
    service = ns["Service"](resource_reader=lambda: ns["ResourceSnapshot"](10**12, 0, 0))

    def measured_decoder() -> str:
        decoder_calls.append("actual-decoder")
        return H

    calls: list[Path] = []

    def decode(source: Path, spec: LocalAudioWindowSpec, destination: Path) -> DecodedLocalPcmReport:
        assert source.read_bytes() == SOURCE and source != destination
        calls.append(source)
        destination.write_bytes(MARKER)
        return cast(DecodedLocalPcmReport, _report(spec))

    monkeypatch.setitem(service.load.__globals__, "decoder_identity_sha256", measured_decoder)
    monkeypatch.setitem(service.load.__globals__, "decode_local_pcm", decode)
    return SimpleNamespace(ns=ns, service=service, profile=profile, asr=asr, vad=vad,
                           calls=calls, model_calls=model_calls, constructions=constructions,
                           decoder_calls=decoder_calls)


@pytest_asyncio.fixture
async def shadow_local_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    case = _startup_case(tmp_path, monkeypatch)
    case.client = TestClient(TestServer(case.ns["create_app"](case.service)))
    await case.client.start_server()
    policy = LocalSpeechWindowPolicy(case.profile.canonical_hash, "asr", H, "vad", H, 700, 350)
    spec = LocalAudioWindowSpec(
        "synthetic-source", _sha(SOURCE), 3, "source-audio", TimeBase(1, 1000),
        TickRange(0, 5000), TickRange(1000, 3000), 1000, 1, H, H,
        100_000, 32, 100_000, 100_000,
    )
    case.request = LocalSpeechWindowRequest(spec, policy, H, 100_000)
    try:
        yield case
    finally:
        await case.client.close()


@pytest.mark.asyncio
async def test_startup_derives_native_and_complete_profile_from_actual_measurements(shadow_local_case: SimpleNamespace) -> None:
    case = shadow_local_case
    assert case.service.ready is True and len(case.constructions) == 1
    assert case.decoder_calls == ["actual-decoder"]
    assert decode_shadow_local_service_profile(case.service.measured_profile) == case.profile
    assert case.service.measured == case.profile.canonical_hash
    assert case.service.measured != case.profile.native_port_identity_sha256
    assert case.service.identities == case.profile.to_mapping()["producers"]
    assert case.service.measured_profile["service_sha256"] == case.ns["service_hash"]()
    assert not case.calls and not case.model_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", (False, True))
async def test_repeated_startup_cancel_drains_model_constructor_before_releasing_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_fails: bool,
) -> None:
    """Preexisting shared startup race: shield must survive every cancellation."""
    case = _startup_case(tmp_path, monkeypatch)
    started, released, finished = threading.Event(), threading.Event(), threading.Event()
    original = case.service.load.__globals__["AutoModel"]

    def blocked(**kwargs: object) -> object:
        started.set()
        try:
            assert released.wait(3)
            if worker_fails:
                raise RuntimeError("synthetic constructor failure after cancellation")
            return original(**kwargs)
        finally:
            finished.set()

    monkeypatch.setitem(case.service.load.__globals__, "AutoModel", blocked)
    task = asyncio.create_task(case.service.load())
    contender = case.ns["HostSingletonLock"](case.service.singleton.path)
    try:
        assert await asyncio.to_thread(started.wait, 1)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and not finished.is_set()
            assert case.service.singleton.fd is not None and case.service.ready is False
            with pytest.raises(RuntimeError, match="already held"):
                contender.acquire()
    finally:
        released.set()
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert await asyncio.to_thread(finished.wait, 1)
    assert isinstance(result[0], RuntimeError if worker_fails else asyncio.CancelledError)
    if worker_fails:
        assert str(result[0]) == "synthetic constructor failure after cancellation"
    assert case.service.singleton.fd is None and case.service.model is None
    assert case.service.ready is False and case.service.measured_profile is None
    contender.acquire()
    contender.release()
    await case.service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", (
    "decoder", "asr_model", "vad_model", "service", "funasr", "torch", "device",
    "native_hash", "decoder_missing", "record", "producer_bound", "bool_gap",
))
async def test_startup_rejects_drift_before_ready_and_releases_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    case = _startup_case(tmp_path, monkeypatch)
    service = case.service
    if change == "decoder":
        monkeypatch.setitem(service.load.__globals__, "decoder_identity_sha256", lambda: OTHER)
    elif change in {"asr_model", "vad_model"}:
        (case.asr if change == "asr_model" else case.vad).joinpath("model.pt").write_bytes(b"changed model")
    elif change == "service":
        monkeypatch.setitem(service.load.__globals__, "service_hash", lambda: OTHER)
    elif change == "funasr":
        monkeypatch.setattr(case.ns["importlib"].metadata, "version", lambda _name: "foreign")
    elif change == "torch":
        monkeypatch.setattr(case.ns["torch"], "__version__", "foreign")
    elif change == "device":
        monkeypatch.setattr(_AutoModel, "actual_device", "mps")
    elif change == "native_hash":
        service.profile["native_port_identity_sha256"] = OTHER
    elif change == "decoder_missing":
        del service.profile["decoder_identity_sha256"]
    elif change == "record":
        service.profile["profile_calibration_sha256"] = H
    elif change == "producer_bound":
        service.profile["producers"][0]["timing_error_bound_tick"] = 1
    else:
        service.profile["utterance_gap_milliseconds"] = True
    with pytest.raises((RuntimeError, ValueError)):
        await service.load()
    assert not service.ready and service.measured_profile is None
    assert service.singleton.fd is None and not case.calls and not case.model_calls
    assert len(case.constructions) == (1 if change == "device" else 0)
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", ("normal", "old-shadow"))
async def test_old_startup_modes_do_not_measure_new_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str,
) -> None:
    case = _startup_case(tmp_path, monkeypatch)
    case.service.profile = (_service_profile(case.ns, case.asr, case.vad) if schema == "normal"
                            else _shadow_calibration_profile(case.ns, case.asr, case.vad))

    def forbidden_decoder() -> str:
        raise AssertionError("old startup must not depend on local decoder")

    monkeypatch.setitem(case.service.load.__globals__, "decoder_identity_sha256", forbidden_decoder)
    await case.service.load()
    try:
        assert case.service.ready is True and not case.decoder_calls
        assert "decoder_identity_sha256" not in case.service.measured_profile
    finally:
        await case.service.close()


@pytest.mark.asyncio
async def test_shadow_route_uses_real_window_chain_and_both_models_same_local_file(shadow_local_case: SimpleNamespace) -> None:
    case = shadow_local_case
    response = await case.client.post(ROUTE, headers=_headers(case.request), data=SOURCE)
    assert response.status == 200
    raw = await response.read()
    measured = project_local_speech_window(decode_local_speech_window_response(raw, case.request))
    assert measured.transcript.words[0].in_tick == 1100
    assert measured.transcript.words[0].out_tick == 1200
    assert measured.speech_activity.segments[0].in_tick == 1050
    assert measured.speech_activity.segments[0].out_tick == 1250
    assert measured.transcript.coverage.in_tick == 1000 and measured.transcript.coverage.out_tick == 3000
    assert [kind for kind, _path in case.model_calls] == ["asr", "vad"]
    assert case.model_calls[0][1] == case.model_calls[1][1]
    assert not case.model_calls[0][1].exists() and not case.calls[0].exists()
    assert len(case.calls) == 1 and case.service.admitted == 0 and not case.service.lock.locked()
    assert "calibration_record_sha256" not in raw.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("/v2/timed-speech-window", "/v1/timed-speech-evidence", "/v1/shadow-calibration-funasr-raw"))
async def test_new_profile_cannot_enter_normal_or_old_shadow_routes(shadow_local_case: SimpleNamespace, path: str) -> None:
    case = shadow_local_case
    response = await case.client.post(path, headers=_headers(case.request), data=SOURCE)
    assert response.status == 409 and not case.calls and case.service.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", ("funasr-measured-profile-v1", "funasr-shadow-calibration-profile-v1"))
async def test_old_profile_cannot_enter_new_shadow_route(shadow_local_case: SimpleNamespace, schema: str) -> None:
    case = shadow_local_case
    case.service.measured_profile["schema_version"] = schema
    response = await case.client.post(ROUTE, headers=_headers(case.request), data=SOURCE)
    assert response.status == 409 and not case.calls and case.service.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", (
    ("service_profile_sha256", OTHER), ("asr_producer_id", "foreign-asr"), ("vad_producer_id", "foreign-vad"),
    ("asr_generation_policy_sha256", OTHER), ("vad_generation_policy_sha256", OTHER),
    ("utterance_gap_milliseconds", 701), ("vad_merge_gap_milliseconds", 351),
))
async def test_every_policy_field_checked_before_upload(
    shadow_local_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, field: str, value: object,
) -> None:
    case = shadow_local_case
    request = replace(case.request, policy=replace(case.request.policy, **{field: value}))

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid policy must not spool")

    monkeypatch.setattr(case.ns["tempfile"], "TemporaryDirectory", forbidden)
    response = await case.client.post(ROUTE, headers=_headers(request), data=b"foreign")
    assert response.status == 409 and not case.calls and not case.model_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,status", (
    ("auth", 401), ("unicode_auth", 401), ("request_hash", 400), ("duplicate", 400),
    ("mode_field", 400), ("noncanonical", 400), ("decoder", 409), ("decoder_changed_after_startup", 409),
    ("actual_decoder_drift", 409),
    ("source_limit", 400), ("response_limit", 400), ("not_ready", 503),
))
async def test_bad_request_never_uploads_or_starts_native(
    shadow_local_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, mode: str, status: int,
) -> None:
    case = shadow_local_case
    request = case.request
    raw: bytes | None = None
    if mode in {"decoder", "decoder_changed_after_startup"}:
        request = replace(request, extraction=replace(request.extraction, decoder_identity_sha256=OTHER))
        if mode == "decoder_changed_after_startup":
            monkeypatch.setitem(case.service.load.__globals__, "decoder_identity_sha256", lambda: OTHER)
    elif mode == "actual_decoder_drift":
        monkeypatch.setitem(case.service.load.__globals__, "decoder_identity_sha256", lambda: OTHER)
    elif mode == "source_limit":
        request = replace(request, extraction=replace(request.extraction, max_source_bytes=case.service.max_request + 1))
    elif mode == "response_limit":
        request = replace(request, max_response_bytes=case.service.max_response + 1)
    elif mode == "not_ready":
        case.service.ready = False
    elif mode == "duplicate":
        raw = json.dumps(request.to_mapping(), sort_keys=True, separators=(",", ":")).encode()
        raw = b'{"binding_sha256":' + json.dumps(H).encode() + b"," + raw[1:]
    elif mode == "mode_field":
        mapping = request.to_mapping()
        mapping["mode"] = "normal"
        raw = json.dumps(mapping).encode()
    elif mode == "noncanonical":
        raw = json.dumps(request.to_mapping(), indent=2).encode()
    headers = _headers(request, raw)
    if mode in {"auth", "unicode_auth"}:
        headers["Authorization"] = "Bearer 错误" if mode == "unicode_auth" else "Bearer wrong"
    elif mode == "request_hash":
        headers["X-Local-Speech-Window-SHA256"] = OTHER

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid request must not spool")

    monkeypatch.setattr(case.ns["tempfile"], "TemporaryDirectory", forbidden)
    response = await case.client.post(ROUTE, headers=headers, data=SOURCE)
    assert response.status == status and not case.calls and case.service.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("bad_hash", "empty", "oversize", "chunked_oversize"))
async def test_upload_failure_cleans_directory_and_does_not_call_models(
    shadow_local_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    case = shadow_local_case
    directories: list[Path] = []
    original = case.ns["tempfile"].TemporaryDirectory

    def spool(*args: object, **kwargs: object) -> Any:
        result = original(*args, **kwargs)
        directories.append(Path(result.name))
        return result

    monkeypatch.setattr(case.ns["tempfile"], "TemporaryDirectory", spool)
    request = replace(case.request, extraction=replace(case.request.extraction, max_source_bytes=len(SOURCE)))
    body = b"" if mode == "empty" else b"foreign"
    if "oversize" in mode:
        body = SOURCE + b"x"

    async def chunks() -> AsyncIterator[bytes]:
        yield body[:3]
        yield body[3:]

    response = await case.client.post(ROUTE, headers=_headers(request), data=chunks() if mode == "chunked_oversize" else body)
    assert response.status == (413 if "oversize" in mode else 400)
    assert not case.calls and not case.model_calls and case.service.admitted == 0
    assert all(not directory.exists() for directory in directories)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("queue", "resource", "proof_too_large"))
async def test_busy_proof_is_request_bound_and_only_pre_admission(
    shadow_local_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    case = shadow_local_case
    if mode == "resource":
        case.service.resource_reader = lambda: case.ns["ResourceSnapshot"](0, 0, 0)
    else:
        case.service.admitted = case.service.queue_capacity
    request = replace(case.request, max_response_bytes=1) if mode == "proof_too_large" else case.request

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("busy request must not spool")

    monkeypatch.setattr(case.ns["tempfile"], "TemporaryDirectory", forbidden)
    response = await case.client.post(ROUTE, headers=_headers(request), data=SOURCE)
    raw = await response.read()
    assert response.status == 503 and not case.calls and not case.model_calls
    if mode == "proof_too_large":
        assert raw == b""
    else:
        proof = decode_local_speech_window_busy_proof(raw, request)
        assert proof.service_profile_sha256 == case.profile.canonical_hash
    if mode == "resource":
        assert response.headers["Retry-After"] == "1" and case.service.admitted == 0
    else:
        assert case.service.admitted == case.service.queue_capacity


@pytest.mark.asyncio
async def test_later_service_unavailable_is_not_a_busy_proof(shadow_local_case: SimpleNamespace) -> None:
    case = shadow_local_case
    original = case.service.run_window_inference

    async def later(path: Path, spec: LocalAudioWindowSpec) -> object:
        await original(path, spec)
        raise web.HTTPServiceUnavailable(text="post-dispatch failure")

    case.service.run_window_inference = later
    response = await case.client.post(ROUTE, headers=_headers(case.request), data=SOURCE)
    raw = await response.read()
    assert response.status == 503 and raw == b"post-dispatch failure"
    with pytest.raises(ValueError):
        decode_local_speech_window_busy_proof(raw, case.request)
    assert len(case.calls) == 1 and len(case.model_calls) == 2 and case.service.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("raw_invalid", "report_drift", "response_bound"))
async def test_failed_measurement_never_becomes_success_or_busy(shadow_local_case: SimpleNamespace, mode: str) -> None:
    case = shadow_local_case
    original = case.service.run_window_inference

    async def changed(path: Path, spec: LocalAudioWindowSpec) -> tuple[object, object, object]:
        report, asr, vad = await original(path, spec)
        if mode == "raw_invalid":
            asr[0]["timestamp"] = [[-1, 10]]
        elif mode == "report_drift":
            report = replace(report, source_sha256=OTHER)
        return report, asr, vad

    case.service.run_window_inference = changed
    request = replace(case.request, max_response_bytes=1) if mode == "response_bound" else case.request
    response = await case.client.post(ROUTE, headers=_headers(request), data=SOURCE)
    assert response.status == 422
    with pytest.raises(ValueError):
        decode_local_speech_window_busy_proof(await response.read(), request)
    assert len(case.calls) == 1 and len(case.model_calls) == 2
    assert case.service.admitted == 0 and not case.calls[0].exists()


@pytest.mark.asyncio
async def test_repeated_cancel_keeps_upload_and_lock_until_worker_finishes(shadow_local_case: SimpleNamespace) -> None:
    case = shadow_local_case
    started, released, finished = threading.Event(), threading.Event(), threading.Event()
    original = case.service.infer_window

    def blocked(path: Path, spec: LocalAudioWindowSpec) -> object:
        started.set()
        assert released.wait(3)
        result = original(path, spec)
        finished.set()
        return result

    class Content:
        async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
            yield SOURCE

    case.service.infer_window = blocked
    request = SimpleNamespace(headers=_headers(case.request), content=Content(), content_length=len(SOURCE))
    task = asyncio.create_task(case.service.shadow_local_window_evidence(request))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and case.service.lock.locked() and case.service.admitted == 1
    finally:
        released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and len(case.calls) == 1 and not case.calls[0].exists()
    assert len(case.model_calls) == 2 and case.service.admitted == 0 and not case.service.lock.locked()


def test_only_shared_builder_defines_local_profile_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _startup_case(tmp_path, monkeypatch)
    mapping = case.profile.to_mapping()
    without_native = {key: value for key, value in mapping.items() if key != "native_port_identity_sha256"}
    assert case.profile.native_port_identity_sha256 == canonical_sha256(without_native)
    assert case.profile.canonical_hash == canonical_sha256(mapping)
    assert case.profile.canonical_hash != case.profile.native_port_identity_sha256
    wire_policy = LocalSpeechWindowPolicy(case.profile.canonical_hash, "asr", H, "vad", H, 700, 350)
    assert "anchors" not in wire_policy.to_mapping()
