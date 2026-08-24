from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest
from autocut_kernel.media import TimeBase

from auto_cut_bot.pipeline.media_preflight import (
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


def namespace(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    f = ModuleType("funasr")
    f.AutoModel = object  # type: ignore[attr-defined]
    t = ModuleType("torch")
    t.__version__ = "test"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", f)
    monkeypatch.setitem(sys.modules, "torch", t)
    return runpy.run_path("deploy/funasr/service.py")


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
    assert len(tr["sentences"]) == 2


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
