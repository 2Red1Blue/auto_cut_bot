"""Real loopback HTTP and synthetic model/decoder facts; not calibration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from autocut_kernel.media.local_audio_window import LocalAudioWindowSpec
from autocut_kernel.media.local_speech_window import (
    DecodedLocalPcmReport,
    LocalSpeechWindowPolicy,
    LocalSpeechWindowRequest,
)
from autocut_kernel.media.local_speech_window_codec import decode_local_speech_window_response
from autocut_kernel.media.local_speech_window_projection import project_local_speech_window
from autocut_kernel.media.types import TickRange, TimeBase

from tests.pipeline.test_funasr_timed_speech import (
    _AutoModel,
    _service_environment,
    _service_profile,
    namespace,
)

H = "sha256:" + "1" * 64
SOURCE = b"synthetic original media"
ROUTE = "/v2/timed-speech-window"


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _headers(request, raw=None):
    raw = _canonical(request.to_mapping()) if raw is None else raw
    return {"Authorization": "Bearer secret",
            "X-Local-Speech-Window-Manifest": base64.b64encode(raw).decode(),
            "X-Local-Speech-Window-SHA256": _sha(raw)}


def _report(spec):
    return DecodedLocalPcmReport(
        spec.source_sha256, spec.canonical_hash, spec.decoder_identity_sha256,
        H, H, spec.expected_samples * spec.channels * 4 + 80,
        spec.sample_rate, spec.channels, spec.expected_samples, 1,
    )


@pytest_asyncio.fixture
async def case(tmp_path, monkeypatch):
    ns = namespace(monkeypatch, _AutoModel)
    monkeypatch.setattr(ns["importlib"].metadata, "version", lambda _name: "test")
    asr, vad = tmp_path / "asr/snapshots/master", tmp_path / "vad/snapshots/v2"
    asr.mkdir(parents=True)
    vad.mkdir(parents=True)
    (asr / "model.pt").write_bytes(b"synthetic ASR")
    (vad / "model.pt").write_bytes(b"synthetic VAD")
    profile = _service_profile(ns, asr, vad)
    _service_environment(monkeypatch, profile, asr, vad)
    service = ns["Service"]()
    monkeypatch.setitem(service.window_evidence.__globals__, "decoder_identity_sha256", lambda: H)
    calls = []

    def infer_window(path, spec):
        assert path.read_bytes() == SOURCE
        calls.append(path)
        return (_report(spec), [{"text": "hello", "words": ["hello"], "timestamp": [[100, 200]]}],
                [{"value": [[50, 250]]}])

    service.infer_window = infer_window
    client = TestClient(TestServer(ns["create_app"](service)))
    await client.start_server()
    policy = LocalSpeechWindowPolicy(
        _sha(_canonical(service.measured_profile)), "asr", H, "vad", H, 700, 350,
    )
    spec = LocalAudioWindowSpec(
        "synthetic-source", _sha(SOURCE), 3, "source-audio", TimeBase(1, 1000),
        TickRange(0, 5000), TickRange(1000, 3000), 1000, 1, H, H,
        100_000, 32, 100_000, 100_000,
    )
    request = LocalSpeechWindowRequest(spec, policy, H, 100_000)
    try:
        yield SimpleNamespace(ns=ns, service=service, client=client, request=request, calls=calls)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_window_route_roundtrips_kernel_projection_and_private_upload(case):
    response = await case.client.post(ROUTE, headers=_headers(case.request), data=SOURCE)
    assert response.status == 200
    decoded = decode_local_speech_window_response(await response.read(), case.request)
    projection = project_local_speech_window(decoded)
    assert projection is not None and decoded.report.sample_count == 2000
    assert len(case.calls) == 1 and not case.calls[0].exists()
    assert case.service.admitted == 0 and not case.service.lock.locked()
    assert case.ns["DecodedLocalPcmReport"] is DecodedLocalPcmReport


@pytest.mark.asyncio
@pytest.mark.parametrize("speech", [False, True])
async def test_empty_lexical_output_preserves_vad_only_or_explicit_silence(case, speech):
    original = case.service.infer_window

    def infer(path, spec):
        report, _asr, _vad = original(path, spec)
        return report, [{"text": "", "words": [], "timestamp": []}], [{"value": [[50, 250]] if speech else []}]

    case.service.infer_window = infer
    response = await case.client.post(ROUTE, headers=_headers(case.request), data=SOURCE)
    assert response.status == 200
    evidence = project_local_speech_window(decode_local_speech_window_response(await response.read(), case.request))
    assert not evidence.transcript.words
    assert evidence.transcript.source_outcome.value == ("no_lexical_content" if speech else "no_speech")
    assert bool(evidence.speech_activity.segments) is speech
    assert evidence.transcript.completeness.sentence.value == "not_applicable"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("service_profile_sha256", "sha256:" + "2" * 64),
    ("asr_producer_id", "foreign-asr"), ("vad_producer_id", "foreign-vad"),
    ("asr_generation_policy_sha256", "sha256:" + "2" * 64),
    ("vad_generation_policy_sha256", "sha256:" + "2" * 64),
    ("utterance_gap_milliseconds", 701), ("vad_merge_gap_milliseconds", 351),
])
async def test_every_measured_policy_field_is_checked_before_body(case, field, value):
    request = replace(case.request, policy=replace(case.request.policy, **{field: value}))
    response = await case.client.post(ROUTE, headers=_headers(request), data=b"bad source")
    assert response.status == 409 and not case.calls and case.service.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,status", [
    ("auth", 401), ("unicode_auth", 401), ("hash", 400), ("missing", 400), ("duplicate", 400),
    ("extra", 400), ("float", 400), ("bool", 400), ("noncanonical", 400),
    ("decoder", 409), ("source_cap", 400), ("response_cap", 400),
    ("shadow", 409), ("not_ready", 503),
])
async def test_bad_request_never_spools_or_calls_models(case, monkeypatch, mutation, status):
    request = case.request
    raw = _canonical(request.to_mapping())
    if mutation in {"extra", "float", "bool"}:
        mapping = request.to_mapping()
        if mutation == "extra":
            mapping["accepted"] = True
        else:
            mapping["max_response_bytes"] = 100000.0 if mutation == "float" else True
        raw = _canonical(mapping)
    elif mutation == "duplicate":
        raw = raw[:-1] + b',"binding_sha256":' + json.dumps(H).encode() + b"}"
    elif mutation == "noncanonical":
        raw += b"\n"
    elif mutation == "decoder":
        request = replace(request, extraction=replace(request.extraction, decoder_identity_sha256="sha256:" + "2" * 64))
    elif mutation == "source_cap":
        request = replace(request, extraction=replace(request.extraction, max_source_bytes=case.service.max_request + 1))
    elif mutation == "response_cap":
        request = replace(request, max_response_bytes=case.service.max_response + 1)
    elif mutation == "shadow":
        case.service.measured_profile["schema_version"] = "funasr-shadow-calibration-profile-v1"
    elif mutation == "not_ready":
        case.service.ready = False
    headers = _headers(request, raw if request is case.request else None)
    if mutation == "auth":
        headers["Authorization"] = "Bearer wrong"
    elif mutation == "unicode_auth":
        headers["Authorization"] = "Bearer 错误"
    elif mutation == "hash":
        headers["X-Local-Speech-Window-SHA256"] = H
    elif mutation == "missing":
        del headers["X-Local-Speech-Window-Manifest"]

    def forbid_spool(*_args, **_kwargs):
        raise AssertionError("invalid request must not spool")

    monkeypatch.setattr(case.ns["tempfile"], "TemporaryDirectory", forbid_spool)
    response = await case.client.post(ROUTE, headers=headers, data=SOURCE)
    assert response.status == status and not case.calls and case.service.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bad_hash", "empty", "oversize", "chunked_oversize"])
async def test_upload_rejected_and_private_directory_removed(case, monkeypatch, mode):
    directories = []
    original = case.ns["tempfile"].TemporaryDirectory

    def spool(*args, **kwargs):
        result = original(*args, **kwargs)
        directories.append(Path(result.name))
        return result

    monkeypatch.setattr(case.ns["tempfile"], "TemporaryDirectory", spool)
    request = replace(case.request, extraction=replace(case.request.extraction, max_source_bytes=len(SOURCE)))
    body = b"" if mode == "empty" else b"foreign"
    if "oversize" in mode:
        body = b"x" * (len(SOURCE) + 1)

    async def chunks():
        yield body[:2]
        yield body[2:]

    response = await case.client.post(ROUTE, headers=_headers(request),
                                     data=chunks() if mode == "chunked_oversize" else body)
    assert response.status == (413 if "oversize" in mode else 400)
    assert not case.calls and case.service.admitted == 0
    assert all(not directory.exists() for directory in directories)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native_invalid", "foreign_report", "response_bound"])
async def test_invalid_output_is_not_returned_as_success(case, mode):
    original = case.service.infer_window

    def infer(path, spec):
        report, asr, vad = original(path, spec)
        if mode == "native_invalid":
            asr[0]["timestamp"] = [[-1, 20]]
        elif mode == "foreign_report":
            report = replace(report, source_sha256="sha256:" + "2" * 64)
        return report, asr, vad

    case.service.infer_window = infer
    request = replace(case.request, max_response_bytes=1) if mode == "response_bound" else case.request
    response = await case.client.post(ROUTE, headers=_headers(request), data=SOURCE)
    assert response.status == 422 and case.service.admitted == 0
    assert len(case.calls) == 1 and not case.calls[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_resource_and_queue_admission_precede_spooling(case, monkeypatch, busy):
    if busy:
        case.service.admitted = case.service.queue_capacity
    else:
        case.service.resource_reader = lambda: case.ns["ResourceSnapshot"](0, 0, 0)

    def forbid_spool(*_args, **_kwargs):
        raise AssertionError("rejected admission must not spool")

    monkeypatch.setattr(case.ns["tempfile"], "TemporaryDirectory", forbid_spool)
    response = await case.client.post(ROUTE, headers=_headers(case.request), data=SOURCE)
    assert response.status == 503 and not case.calls
    assert case.service.admitted == (case.service.queue_capacity if busy else 0)


@pytest.mark.asyncio
async def test_direct_handler_repeated_cancel_drains_native_then_cleans_upload(case):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    paths = []

    def infer(path, spec):
        paths.append(path)
        started.set()
        assert release.wait(3)
        finished.set()
        return _report(spec), [], []

    class Content:
        async def iter_chunked(self, _size):
            yield SOURCE

    case.service.infer_window = infer
    req = SimpleNamespace(headers=_headers(case.request), content=Content(), content_length=len(SOURCE))
    task = asyncio.create_task(case.service.window_evidence(req))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert case.service.lock.locked() and case.service.admitted == 1 and paths[0].exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and case.service.admitted == 0 and not paths[0].exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_queue_release_cannot_leak_admission(case):
    releasing, release_allowed = asyncio.Event(), asyncio.Event()
    original = case.service.release

    async def release():
        releasing.set()
        await release_allowed.wait()
        await original()

    class Content:
        async def iter_chunked(self, _size):
            yield SOURCE

    case.service.release = release
    req = SimpleNamespace(headers=_headers(case.request), content=Content(), content_length=len(SOURCE))
    task = asyncio.create_task(case.service.window_evidence(req))
    await asyncio.wait_for(releasing.wait(), 1)
    try:
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and case.service.admitted == 1
    finally:
        release_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert case.service.admitted == 0 and not case.calls[0].exists()


@pytest.mark.asyncio
async def test_http_runs_exact_synthetic_frame_extraction_and_both_models_read_local_wav(case, monkeypatch):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    values = np.arange(-2500, 2500, dtype=np.int16).reshape(1, 5000)
    frame = SimpleNamespace(
        pts=0, time_base=Fraction(1, 1000), sample_rate=1000, samples=5000,
        layout=SimpleNamespace(channels=(0,)), format=SimpleNamespace(name="s16", is_planar=False),
        planes=(SimpleNamespace(buffer_size=values.nbytes),), to_ndarray=lambda: values,
    )
    stream = SimpleNamespace(index=3, type="audio")

    class Container:
        streams = (stream,)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def decode(self, selected):
            assert selected is stream
            yield frame

    def opened(file, *, mode, io_open):
        assert mode == "r" and file.read() == SOURCE and callable(io_open)
        file.seek(0)
        return Container()

    fake_av = SimpleNamespace(open=opened, __version__="synthetic-av", library_versions={"synthetic": (1, 0, 0)})
    globals_ = case.service.window_evidence.__globals__
    monkeypatch.setitem(globals_, "_pcm_dependencies", lambda: (fake_av, np, sf))
    monkeypatch.setitem(globals_, "decoder_identity_sha256", case.ns["decoder_identity_sha256"])
    request = replace(case.request, extraction=replace(
        case.request.extraction, decoder_identity_sha256=case.ns["decoder_identity_sha256"](),
    ))
    case.service.infer_window = type(case.service).infer_window.__get__(case.service)
    wav_paths = []

    def read_wav(path):
        path = Path(path)
        data, rate = sf.read(path, dtype="float32", always_2d=True)
        np.testing.assert_array_equal(data, values[:, 1000:3000].T.astype("float32") / 32768)
        assert rate == 1000 and path.suffix == ".wav"
        wav_paths.append(path)

    def generate(*, input, output_timestamp):
        assert output_timestamp is True
        read_wav(input)
        return [{"text": "hello", "words": ["hello"], "timestamp": [[100, 200]]}]

    def inference(path, **_kwargs):
        read_wav(path)
        return [{"value": [[50, 250]]}]

    monkeypatch.setattr(case.service.model, "generate", generate)
    monkeypatch.setattr(case.service.model, "inference", inference)
    response = await case.client.post(ROUTE, headers=_headers(request), data=SOURCE)
    assert response.status == 200
    decoded = decode_local_speech_window_response(await response.read(), request)
    assert decoded.report.sample_count == 2000 and decoded.report.decoded_frames == 1
    assert len(wav_paths) == 2 and wav_paths[0] == wav_paths[1] and not wav_paths[0].exists()
    assert case.service.admitted == 0
