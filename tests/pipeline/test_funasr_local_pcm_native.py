"""Opt-in desktop codec acceptance; no real speech model or HTTP service.

Never enable this on the development Mac. The default suite collects/skips it.
It encodes tiny AAC/MP4 files, then compares actual decoded samples with the
local WAV presented to the two mocked speech models.
"""

from __future__ import annotations

import hashlib
import os
import runpy
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import pytest
from autocut_kernel.media.local_audio_window import LocalAudioWindowSpec
from autocut_kernel.media.types import TickRange, TimeBase

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOCUT_RUN_NATIVE_AUDIO_TESTS") != "1",
    reason="real codec acceptance runs only on the explicitly selected desktop",
)

HASH = "sha256:" + "1" * 64


def _namespace(monkeypatch: pytest.MonkeyPatch):
    fake_funasr, fake_torch = ModuleType("funasr"), ModuleType("torch")
    setattr(fake_funasr, "AutoModel", object)
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    return runpy.run_path("deploy/funasr/service.py")


@pytest.mark.parametrize("rate", [44_100, 48_000])
@pytest.mark.parametrize("offset", [0, 9_600])
def test_real_aac_window_matches_decoded_source_and_both_model_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rate: int, offset: int,
):
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    ns = _namespace(monkeypatch)
    source = tmp_path / "tiny-source.mp4"
    with av.open(str(source), "w") as container:
        stream = container.add_stream("aac", rate=rate)
        stream.layout = "mono"
        for ordinal in range(8):
            waveform = (np.sin(np.arange(1024) * 0.023 + ordinal) * 0.4).astype("float32")
            frame = av.AudioFrame.from_ndarray(waveform.reshape(1, -1), format="fltp", layout="mono")
            frame.sample_rate, frame.pts, frame.time_base = rate, offset + ordinal * 1024, Fraction(1, rate)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    with av.open(str(source)) as container:
        stream_index = container.streams.audio[0].index
        frames = list(container.decode(audio=0))
    assert len(frames) >= 4
    positions = [Fraction(f.pts) * f.time_base * rate for f in frames]
    assert all(x.denominator == 1 for x in positions)
    source_start, source_end = int(positions[0]), int(positions[-1]) + frames[-1].samples
    if offset:
        assert source_start > 0  # The test must actually exercise a nonzero source PTS.
    requested = TickRange(int(positions[1]) + 128, int(positions[-2]) + 256)
    spec = LocalAudioWindowSpec(
        "native-codec-fixture", "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        stream_index, "source-audio", TimeBase(1, rate), TickRange(source_start, source_end),
        requested, rate, 1, HASH, ns["decoder_identity_sha256"](),
        1_000_000, 100, 1_000_000, 1_000_000,
    )
    expected = np.concatenate([
        f.to_ndarray().reshape(-1)[max(0, requested.start_pts - int(start)):
                                  min(f.samples, requested.end_pts - int(start))]
        for f, start in zip(frames, positions, strict=True)
        if int(start) < requested.end_pts and requested.start_pts < int(start) + f.samples
    ])
    seen_paths: list[Path] = []

    class InspectingModel:
        vad_model = object()
        vad_kwargs: dict[str, object] = {}

        def _inspect(self, path):
            local = Path(path)
            assert local != source and local.suffix == ".wav"
            actual, actual_rate = sf.read(local, dtype="float32", always_2d=False)
            assert actual_rate == rate
            np.testing.assert_array_equal(actual, expected)
            seen_paths.append(local)

        def generate(self, *, input, output_timestamp):
            assert output_timestamp is True
            self._inspect(input)
            return [{"text": "", "words": [], "timestamp": []}]

        def inference(self, path, *, model, kwargs):
            assert model is self.vad_model and kwargs == self.vad_kwargs
            self._inspect(path)
            return [{"value": []}]

    service = object.__new__(ns["Service"])
    service.model = InspectingModel()
    report, asr, vad = service.infer_window(source, spec)
    assert len(seen_paths) == 2 and seen_paths[0] == seen_paths[1]
    assert not seen_paths[0].exists()
    assert source.exists()
    assert report.sample_count == len(expected) == requested.end_pts - requested.start_pts
    assert report.spec_sha256 == spec.canonical_hash
    assert asr == [{"text": "", "words": [], "timestamp": []}] and vad == [{"value": []}]
