"""End-to-end window wire: real port transport against an ephemeral native service.

The client under test is ``FunASRHttpLocalSpeechWindowPort`` with its production
``HttpxFileTransport`` (no mocked HTTP layer).  The server is the real
``deploy/funasr/service.py`` mounted on an aiohttp ``TestServer``.  Fixture and
identity helpers are reused from ``test_funasr_window_endpoint`` rather than
duplicated.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest
from autocut_kernel.media.local_speech_window_busy import decode_local_speech_window_busy_proof
from autocut_kernel.media.local_speech_window_codec import decode_local_speech_window_response

from auto_cut_bot.pipeline.media_preflight.funasr_window_http import (
    FunASRHttpLocalSpeechWindowPort,
    LocalSpeechWindowBusyError,
)
from auto_cut_bot.pipeline.media_preflight.models import LocalMediaToolError
from tests.pipeline.test_funasr_window_endpoint import ROUTE, SOURCE, _sha
from tests.pipeline.test_funasr_window_endpoint import case as window_case  # noqa: F401


def _port(case, shared_token: str = "secret", max_response_bytes: int | None = None):
    return FunASRHttpLocalSpeechWindowPort(
        endpoint_url=str(case.client.make_url(ROUTE)),
        shared_token=shared_token,
        timeout_seconds=5,
        max_response_bytes=(
            max_response_bytes if max_response_bytes is not None else case.request.max_response_bytes
        ),
    )


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(SOURCE)
    return source


@pytest.mark.asyncio
async def test_port_produces_canonical_manifest_roundtrip_through_real_transport(
    tmp_path, window_case  # noqa: F811 - imported pytest fixture
):
    source = _source(tmp_path)
    result = await asyncio.to_thread(_port(window_case).produce, source, window_case.request)

    # The service accepted the canonical manifest headers and returned bytes the
    # strict decoder closes over the exact request hash.
    decoded = decode_local_speech_window_response(result.raw_response, window_case.request)
    assert decoded.request == window_case.request
    assert decoded.report.sample_count == 2000
    assert decoded.raw_response == result.raw_response
    assert result.evidence.decoded.response_sha256 == _sha(result.raw_response)

    # Original source clock local evidence: word/VAD ticks land inside the
    # requested (1000, 3000) window, not the source origin.
    transcript = result.evidence.transcript
    assert transcript.context.origin_tick == 1000
    assert transcript.context.duration_tick == 2000
    assert transcript.words[0].in_tick == 1100
    assert transcript.words[0].out_tick == 1200
    speech = result.evidence.speech_activity
    assert speech.segments[0].in_tick == 1050
    assert speech.segments[0].out_tick == 1250

    # Exactly one native dispatch; the private spool and admission both release.
    assert len(window_case.calls) == 1
    assert not window_case.calls[0].exists()
    assert window_case.service.admitted == 0
    assert not window_case.service.lock.locked()


@pytest.mark.asyncio
async def test_port_extracts_synthetic_frames_and_both_models_read_identical_local_wav(
    tmp_path, window_case, monkeypatch  # noqa: F811 - imported pytest fixture
):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    values = np.arange(-2500, 2500, dtype=np.int16).reshape(1, 5000)
    frame = SimpleNamespace(
        pts=0,
        time_base=Fraction(1, 1000),
        sample_rate=1000,
        samples=5000,
        layout=SimpleNamespace(channels=(0,)),
        format=SimpleNamespace(name="s16", is_planar=False),
        planes=(SimpleNamespace(buffer_size=values.nbytes),),
        to_ndarray=lambda: values,
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

    fake_av = SimpleNamespace(
        open=opened, __version__="synthetic-av", library_versions={"synthetic": (1, 0, 0)}
    )
    globals_ = window_case.service.window_evidence.__globals__
    monkeypatch.setitem(globals_, "_pcm_dependencies", lambda: (fake_av, np, sf))
    monkeypatch.setitem(globals_, "decoder_identity_sha256", window_case.ns["decoder_identity_sha256"])
    request = replace(
        window_case.request,
        extraction=replace(
            window_case.request.extraction,
            decoder_identity_sha256=window_case.ns["decoder_identity_sha256"](),
        ),
    )
    # Restore the real extraction seam so the endpoint decodes actual frames and
    # both model callbacks read the identical freshly written local WAV.
    window_case.service.infer_window = type(window_case.service).infer_window.__get__(
        window_case.service
    )
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

    monkeypatch.setattr(window_case.service.model, "generate", generate)
    monkeypatch.setattr(window_case.service.model, "inference", inference)

    result = await asyncio.to_thread(_port(window_case).produce, _source(tmp_path), request)

    decoded = decode_local_speech_window_response(result.raw_response, request)
    assert decoded.report.sample_count == 2000
    assert decoded.report.decoded_frames == 1
    assert result.evidence.transcript.words[0].in_tick == 1100
    assert result.evidence.transcript.words[0].out_tick == 1200
    assert result.evidence.speech_activity.segments[0].in_tick == 1050
    assert result.evidence.speech_activity.segments[0].out_tick == 1250
    assert len(wav_paths) == 2 and wav_paths[0] == wav_paths[1] and not wav_paths[0].exists()
    assert window_case.service.admitted == 0


@pytest.mark.asyncio
async def test_wrong_token_never_dispatches(
    tmp_path, window_case  # noqa: F811 - imported pytest fixture
):
    with pytest.raises(LocalMediaToolError) as error:
        await asyncio.to_thread(
            _port(window_case, shared_token="wrong").produce, _source(tmp_path), window_case.request
        )
    assert "window HTTP failure 401" in str(error.value)
    assert not window_case.calls and window_case.service.admitted == 0


@pytest.mark.asyncio
async def test_measured_policy_drift_never_dispatches(
    tmp_path, window_case  # noqa: F811 - imported pytest fixture
):
    drifted = replace(
        window_case.request, policy=replace(window_case.request.policy, utterance_gap_milliseconds=701)
    )
    with pytest.raises(LocalMediaToolError) as error:
        await asyncio.to_thread(_port(window_case).produce, _source(tmp_path), drifted)
    assert "window HTTP failure 409" in str(error.value)
    assert not window_case.calls and window_case.service.admitted == 0


@pytest.mark.asyncio
async def test_source_hash_mismatch_never_dispatches(
    tmp_path, window_case  # noqa: F811 - imported pytest fixture
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"foreign source bytes")
    with pytest.raises(LocalMediaToolError) as error:
        await asyncio.to_thread(_port(window_case).produce, source, window_case.request)
    assert "window HTTP failure 400" in str(error.value)
    assert not window_case.calls and window_case.service.admitted == 0


@pytest.mark.asyncio
async def test_busy_queue_returns_busy_code_without_model_call(
    tmp_path, window_case  # noqa: F811 - imported pytest fixture
):
    window_case.service.admitted = window_case.service.queue_capacity
    window_case.service.resource_reader = lambda: window_case.ns["ResourceSnapshot"](10**12, 0, 0)
    with pytest.raises(LocalSpeechWindowBusyError) as error:
        await asyncio.to_thread(_port(window_case).produce, _source(tmp_path), window_case.request)
    assert error.value.code == "TIMED_SPEECH_BUSY"
    assert decode_local_speech_window_busy_proof(error.value.raw_response, window_case.request) == error.value.proof
    assert not window_case.calls and window_case.service.admitted == window_case.service.queue_capacity


@pytest.mark.asyncio
async def test_resource_busy_roundtrips_exact_pre_dispatch_proof(
    tmp_path, window_case,  # noqa: F811 - imported pytest fixture
):
    window_case.service.resource_reader = lambda: window_case.ns["ResourceSnapshot"](0, 0, 0)
    with pytest.raises(LocalSpeechWindowBusyError) as error:
        await asyncio.to_thread(_port(window_case).produce, _source(tmp_path), window_case.request)
    error.value.proof.assert_matches(window_case.request)
    assert error.value.raw_response == error.value.proof.to_bytes()
    assert not window_case.calls and window_case.service.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["not_ready", "proof_cannot_fit", "post_dispatch", "post_dispatch_oversize"])
async def test_unproven_service_503_never_authorizes_client_retry(
    tmp_path, window_case, mode,  # noqa: F811 - imported pytest fixture
):
    request = window_case.request
    if mode == "not_ready":
        window_case.service.ready = False
    elif mode == "proof_cannot_fit":
        request = replace(request, max_response_bytes=1)
        window_case.service.admitted = window_case.service.queue_capacity
    else:
        original = window_case.service.run_window_inference

        async def fail_after_dispatch(path, spec):
            await original(path, spec)
            text = "secret" * request.max_response_bytes if mode.endswith("oversize") else "post-dispatch failure"
            raise window_case.ns["web"].HTTPServiceUnavailable(text=text)

        window_case.service.run_window_inference = fail_after_dispatch
    with pytest.raises(LocalMediaToolError) as error:
        await asyncio.to_thread(_port(window_case).produce, _source(tmp_path), request)
    assert error.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert not isinstance(error.value, LocalSpeechWindowBusyError)
    assert "secret" not in str(error.value)
    if mode.startswith("post_dispatch"):
        assert len(window_case.calls) == 1 and not window_case.calls[0].exists()
        assert window_case.service.admitted == 0
    else:
        assert not window_case.calls
