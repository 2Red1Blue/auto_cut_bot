"""Synthetic decoded frames and fake models; real WAV I/O, no native decode."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest
from autocut_kernel.media.local_audio_window import LocalAudioWindowError, LocalAudioWindowSpec
from autocut_kernel.media.types import TickRange, TimeBase

from tests.pipeline.test_funasr_timed_speech import namespace

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class Frame:
    def __init__(self, pts, values, *, name="s16", channels=2, rate=8, base=Fraction(1, 8)):
        self.pts, self.time_base, self.sample_rate = pts, base, rate
        self.layout = SimpleNamespace(channels=tuple(range(channels)))
        self.format = SimpleNamespace(name=name, is_planar=name.endswith("p"))
        self.values = values
        self.samples = values.size // channels
        self.planes = (SimpleNamespace(buffer_size=values.nbytes),)
        self.conversions = 0

    def to_ndarray(self):
        self.conversions += 1
        return self.values


class Container:
    def __init__(self, frames, stream_index=3):
        self.frames = frames
        self.streams = (SimpleNamespace(index=stream_index, type="audio"),)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def decode(self, stream):
        assert stream is self.streams[0]
        yield from self.frames


def _case(tmp_path, monkeypatch, *, frames=None, requested=(2, 6)):
    ns = namespace(monkeypatch)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic source, deliberately not a decodable MP4")
    if frames is None:
        frames = (
            Frame(0, np.arange(-8, 0, dtype=np.int16).reshape(1, 8)),
            Frame(4, np.arange(0, 8, dtype=np.int16).reshape(1, 8)),
        )
    container = Container(frames)

    def opened(stream, *, mode, io_open):
        assert mode == "r" and stream.read() == source.read_bytes()
        assert callable(io_open)
        stream.seek(0)
        return container

    monkeypatch.setattr(av, "open", opened)
    spec = LocalAudioWindowSpec(
        "synthetic-source", _sha(source.read_bytes()), 3, "audio-3", TimeBase(1, 8),
        TickRange(0, 8), TickRange(*requested), 8, 2, "sha256:" + "1" * 64,
        ns["decoder_identity_sha256"](), 4096, 8, 4096, 4096,
    )
    return ns, source, tmp_path / "window.wav", spec, container


def test_exact_cross_frame_samples_report_and_determinism(tmp_path, monkeypatch):
    ns, source, output, spec, container = _case(tmp_path, monkeypatch)
    report = ns["decode_local_pcm"](source, spec, output)
    values, rate = sf.read(output, dtype="float32", always_2d=True)
    expected = np.arange(-4, 4, dtype=np.float32).reshape(4, 2) / 32768
    np.testing.assert_array_equal(values, expected)
    assert rate == 8 and container.closed
    assert report.sample_count == 4 and report.channels == 2 and report.decoded_frames == 2
    assert report.pcm_sha256 == _sha(expected.astype("<f4").tobytes())
    assert report.wav_sha256 == _sha(output.read_bytes())
    wav_bytes = output.read_bytes()
    peak = wav_bytes.index(b"PEAK")
    assert wav_bytes[peak + 12:peak + 16] == b"\0" * 4
    assert report.wav_byte_length == output.stat().st_size
    assert report.source_sha256 == spec.source_sha256 and report.spec_sha256 == spec.canonical_hash
    assert "path" not in json.dumps(report.to_mapping())
    second = ns["decode_local_pcm"](source, spec, tmp_path / "second.wav")
    assert second == report and second.canonical_hash == report.canonical_hash


@pytest.mark.parametrize(("name", "dtype", "values", "expected"), [
    ("u8", "uint8", [0, 128, 255, 64], [-1, 0, 127 / 128, -.5]),
    ("s16", "int16", [-32768, 0, 16384, 32767], [-1, 0, .5, 32767 / 32768]),
    ("s32", "int32", [-2147483648, 0, 1073741824, 0], [-1, 0, .5, 0]),
    ("s64", "int64", [-(2**63), 0, 2**62, 0], [-1, 0, .5, 0]),
    ("flt", "float32", [-1, 0, .5, 1], [-1, 0, .5, 1]),
    ("dbl", "float64", [-1, 0, .5, 1], [-1, 0, .5, 1]),
])
@pytest.mark.parametrize("planar", [False, True])
def test_explicit_pcm_conversion_preserves_channels(tmp_path, monkeypatch, name, dtype, values, expected, planar):
    array = np.array(values, dtype=dtype).reshape(2, 2)
    frame = Frame(0, array.T.copy() if planar else array.reshape(1, 4), name=name + ("p" if planar else ""))
    ns, source, output, spec, _ = _case(tmp_path, monkeypatch, frames=(frame,), requested=(0, 2))
    ns["decode_local_pcm"](source, spec, output)
    actual, _ = sf.read(output, dtype="float32", always_2d=True)
    np.testing.assert_array_equal(actual, np.array(expected, dtype=np.float32).reshape(2, 2))


@pytest.mark.parametrize("change", [
    {"source_sha256": "sha256:" + "2" * 64},
    {"decoder_identity_sha256": "sha256:" + "2" * 64},
    {"max_source_bytes": 1}, {"audio_stream_index": 9},
    {"max_decode_frames": 1}, {"max_frame_bytes": 1},
])
def test_identity_and_budget_failures_leave_no_output(tmp_path, monkeypatch, change):
    ns, source, output, spec, _ = _case(tmp_path, monkeypatch)
    with pytest.raises((LocalAudioWindowError, OSError)):
        ns["decode_local_pcm"](source, replace(spec, **change), output)
    assert not output.exists()


@pytest.mark.parametrize("failure", ["missing_pts", "missing_base", "gap", "overlap", "truncated", "nan", "overflow", "dtype", "shape", "rate", "channels", "planes"])
def test_malformed_frames_fail_and_cleanup(tmp_path, monkeypatch, failure):
    frames = [Frame(0, np.zeros((1, 8), dtype=np.float64), name="dbl"), Frame(4, np.zeros((1, 8), dtype=np.float64), name="dbl")]
    if failure == "missing_pts":
        frames[0].pts = None
    elif failure == "missing_base":
        frames[0].time_base = None
    elif failure in {"gap", "overlap"}:
        frames[1].pts = 5 if failure == "gap" else 3
    elif failure == "truncated":
        frames.pop()
    elif failure in {"nan", "overflow"}:
        frames[0].values[0, 4] = np.nan if failure == "nan" else 1e300
    elif failure == "dtype":
        frames[0].values = frames[0].values.astype(np.int64)
    elif failure == "shape":
        frames[0].values = frames[0].values.reshape(2, 4)
    elif failure == "rate":
        frames[0].sample_rate = 16
    elif failure == "channels":
        frames[0].layout.channels = (0,)
    else:
        frames[0].planes = (SimpleNamespace(buffer_size=999999),)
    ns, source, output, spec, _ = _case(tmp_path, monkeypatch, frames=frames)
    with pytest.raises(ValueError):
        ns["decode_local_pcm"](source, spec, output)
    assert not output.exists()


def test_prefix_not_converted_and_no_frames_after_complete(tmp_path, monkeypatch):
    frames = (Frame(0, np.zeros((1, 8), dtype=np.int16)), Frame(4, np.ones((1, 8), dtype=np.int16)), object())
    ns, source, output, spec, _ = _case(tmp_path, monkeypatch, frames=frames, requested=(4, 8))
    report = ns["decode_local_pcm"](source, spec, output)
    assert frames[0].conversions == 0 and frames[1].conversions == 1
    assert report.decoded_frames == 2


def test_negative_source_ticks_and_mixed_frame_time_bases(tmp_path, monkeypatch):
    frames = (
        Frame(-16, np.arange(-16, 0, dtype=np.int16).reshape(1, 16), base=Fraction(1, 16)),
        Frame(0, np.arange(0, 16, dtype=np.int16).reshape(1, 16)),
    )
    ns, source, output, spec, _ = _case(tmp_path, monkeypatch, frames=frames)
    spec = replace(spec, source_range=TickRange(-8, 8), requested_range=TickRange(-2, 2))
    report = ns["decode_local_pcm"](source, spec, output)
    values, _ = sf.read(output, dtype="float32", always_2d=True)
    np.testing.assert_array_equal(values, np.arange(-4, 4, dtype=np.float32).reshape(4, 2) / 32768)
    assert report.sample_count == 4


def test_source_symlink_rejected_without_output(tmp_path, monkeypatch):
    ns, source, output, spec, _ = _case(tmp_path, monkeypatch)
    link = tmp_path / "link.mp4"
    link.symlink_to(source)
    with pytest.raises(OSError):
        ns["decode_local_pcm"](link, spec, output)
    assert not output.exists() and source.is_file()


def test_source_descriptor_not_reopened_after_path_substitution(tmp_path, monkeypatch):
    ns, source, output, spec, container = _case(tmp_path, monkeypatch)
    original = source.read_bytes()

    def opened(stream, *, mode, io_open):
        source.rename(tmp_path / "original.mp4")
        source.write_bytes(b"foreign replacement")
        assert stream.read() == original
        stream.seek(0)
        return container

    monkeypatch.setattr(av, "open", opened)
    with pytest.raises(LocalAudioWindowError, match="changed"):
        ns["decode_local_pcm"](source, spec, output)
    assert source.read_bytes() == b"foreign replacement" and not output.exists()


def test_same_descriptor_mutation_rejected(tmp_path, monkeypatch):
    ns, source, output, spec, container = _case(tmp_path, monkeypatch)

    def opened(stream, *, mode, io_open):
        source.write_bytes(b"mutated in place")
        return container

    monkeypatch.setattr(av, "open", opened)
    with pytest.raises(LocalAudioWindowError, match="changed"):
        ns["decode_local_pcm"](source, spec, output)
    assert not output.exists()


@pytest.mark.parametrize("url", ["file:///private/sidecar", "https://example.invalid/audio"])
def test_secondary_container_resource_open_is_denied(tmp_path, monkeypatch, url):
    ns, source, output, spec, _ = _case(tmp_path, monkeypatch)

    def opened(stream, *, mode, io_open):
        return io_open(url, 1, {})

    monkeypatch.setattr(av, "open", opened)
    with pytest.raises(LocalAudioWindowError, match="secondary"):
        ns["decode_local_pcm"](source, spec, output)
    assert not output.exists()


def test_preexisting_output_and_foreign_replacement_are_never_deleted(tmp_path, monkeypatch):
    ns, source, output, spec, container = _case(tmp_path, monkeypatch)
    output.write_bytes(b"preexisting")
    with pytest.raises(FileExistsError):
        ns["decode_local_pcm"](source, spec, output)
    assert output.read_bytes() == b"preexisting"
    output.unlink()

    def frames(_stream):
        output.rename(tmp_path / "owned.wav")
        output.write_bytes(b"foreign replacement")
        raise LocalAudioWindowError("synthetic failure")
        yield  # pragma: no cover

    monkeypatch.setattr(container, "decode", frames)
    with pytest.raises(LocalAudioWindowError):
        ns["decode_local_pcm"](source, spec, output)
    assert output.read_bytes() == b"foreign replacement"


def test_decoder_identity_binds_actual_libraries_and_planner_bytes(monkeypatch):
    ns = namespace(monkeypatch)
    identity = ns["decoder_identity"]()
    assert identity["versions"]["pyav"] == av.__version__
    assert identity["versions"]["libsndfile"] == sf.__libsndfile_version__
    assert identity["libav_versions"] == {key: list(value) for key, value in av.library_versions.items()}
    for item in identity["planner_sources"]:
        raw = (Path("packages/autocut-kernel/src/autocut_kernel") / item["path"]).read_bytes()
        assert item["sha256"] == _sha(raw)
    expected = _sha(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
    assert ns["decoder_identity_sha256"]() == expected
    monkeypatch.setattr(av, "__version__", "different-decoder")
    assert ns["decoder_identity_sha256"]() != expected


def test_both_models_receive_only_local_float_wav_and_cleanup(tmp_path, monkeypatch):
    ns, source, _output, spec, _ = _case(tmp_path, monkeypatch)
    service = ns["Service"].__new__(ns["Service"])
    calls = []

    def model_read(path):
        path = Path(path)
        assert path != source and path.suffix == ".wav"
        values, rate = sf.read(path, dtype="float32", always_2d=True)
        assert values.shape == (4, 2) and rate == 8
        calls.append(path)
        return [{"synthetic": True}]

    service.model = SimpleNamespace(
        generate=lambda *, input, output_timestamp: model_read(input),
        inference=lambda path, **kwargs: model_read(path), vad_model=object(), vad_kwargs={},
    )
    report, _, _ = service.infer_window(source, spec)
    assert report.sample_count == 4 and len(calls) == 2 and calls[0] == calls[1]
    assert not calls[0].exists()


@pytest.mark.asyncio
async def test_cancellation_retains_lock_until_window_thread_finishes(tmp_path, monkeypatch):
    ns = namespace(monkeypatch)
    service = ns["Service"].__new__(ns["Service"])
    service.lock, service.timeout = asyncio.Lock(), 5
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def window(*_args):
        started.set()
        assert release.wait(5)
        finished.set()
        return None, {}, {}

    service.infer_window = window
    task = asyncio.create_task(service.run_window_inference(tmp_path / "source", object()))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert service.lock.locked() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and not service.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [False, True])
async def test_repeated_cancellation_cannot_unlock_live_worker(tmp_path, monkeypatch, local):
    ns = namespace(monkeypatch)
    service = ns["Service"].__new__(ns["Service"])
    service.lock, service.timeout, service.ready = asyncio.Lock(), 2, True
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def infer(*_args):
        started.set()
        assert release.wait(3)
        finished.set()
        return (None, {}, {}) if local else ({}, {})

    service.infer_window = service.infer = infer
    operation = (service.run_window_inference(tmp_path / "source", object()) if local
                 else service.run_inference(tmp_path / "source"))
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(started.wait, 1)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and service.lock.locked() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and not service.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [False, True])
async def test_cancelled_worker_keeps_original_fatal_deadline(tmp_path, monkeypatch, local):
    ns = namespace(monkeypatch)
    service = ns["Service"].__new__(ns["Service"])
    service.lock, service.timeout, service.ready = asyncio.Lock(), 0.08, True
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    exits = []

    def infer(*_args):
        started.set()
        assert release.wait(3)
        finished.set()
        return (None, {}, {}) if local else ({}, {})

    def fatal_exit(code):
        exits.append((code, service.lock.locked(), finished.is_set(), service.ready))

    service.infer_window = service.infer = infer
    service._fatal_exit = fatal_exit
    operation = (service.run_window_inference(tmp_path / "source", object()) if local
                 else service.run_inference(tmp_path / "source"))
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(RuntimeError, match="fatal exit returned"):
            await asyncio.wait_for(task, 0.5)
        assert exits == [(70, True, False, False)]
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
