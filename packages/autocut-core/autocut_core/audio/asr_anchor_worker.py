#!/usr/bin/env python3
"""ASR Anchor worker: runs in .venv-audio-boundary, produces word timestamps + utterances + VAD.

Called via subprocess from ASRAnchorDetector.detect().
Uses SenseVoice-Small for word timestamps and fsmn-vad for speech segments.

Architecture (three-tier cascade / 三层确定性漏斗):
  Tier 1: Word-level onset from SenseVoice ASR (precision ~30-50ms)
  Tier 2: VAD segment onset from fsmn-vad (for non-verbal: screams, gasps, whispers)
  Tier 3: Pure visual (PySceneDetect) when no audio detected — handled by caller, not here.

Key design decisions:
  - SenseVoice runs WITHOUT punctuation model (punc adds ~28s load time and slows inference;
    we don't need punctuation for onset detection, only word timestamps)
  - SenseVoice with output_timestamp=True returns ABSOLUTE timestamps (ms) relative to file start
    when VAD splits internally (FunASR pipeline handles offset correction)
  - Utterances are segmented by word-gap > word_gap_threshold (default 0.7s)
    This is MORE precise than VAD segments which merge 10-30s blocks
  - VAD segments are loaded in a separate lightweight pass (model is only 1.6MB, inference ~1-2s)
    Provides Tier-2 fallback for non-verbal vocalizations (screams, gasps) where ASR produces no words
  - Sets OMP_NUM_THREADS=4 for optimal CPU inference

Usage:
    python asr_anchor_worker.py --source <video> --work-dir <dir> --out <json> [--device cpu]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path


def _ffmpeg() -> str:
    for c in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        try:
            subprocess.run([c, "-version"], capture_output=True, check=True)
            return c
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise RuntimeError("ffmpeg not found")


def _ffprobe(ffmpeg: str) -> str:
    return ffmpeg.replace("ffmpeg", "ffprobe")


def _extract_audio(source: Path, work_dir: Path, sr: int = 16000) -> Path:
    """Extract mono 16kHz PCM audio from video/audio file."""
    import hashlib
    src_hash = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:12]
    out = work_dir / f"audio_{src_hash}.wav"
    if out.is_file() and out.stat().st_size > 1000:
        return out
    ffmpeg = _ffmpeg()
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-vn", "-ac", "1", "-ar", str(sr),
            "-c:a", "pcm_s16le",
            str(out),
        ],
        check=True, capture_output=True,
    )
    return out


def _has_audio(source: Path) -> bool:
    ffmpeg = _ffmpeg()
    ffprobe = _ffprobe(ffmpeg)
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "json", str(source)],
        capture_output=True, text=True,
    )
    return bool(json.loads(probe.stdout or "{}").get("streams"))


# Default model paths (user's local modelscope cache)
_DEFAULT_SENSEVOICE = os.path.expanduser(
    "~/.cache/modelscope/models/iic--SenseVoiceSmall/snapshots/master"
)
_DEFAULT_VAD = os.path.expanduser(
    "~/.cache/modelscope/models/iic--speech_fsmn_vad_zh-cn-16k-common-pytorch/snapshots/v2.0.4"
)

# Punctuation / special tokens to strip from words
_PUNCT_CHARS = set(",.!?;:，。！？；：、、\"''()（）[]【】《》<>~—-―…·")


def _is_punctuation(word: str) -> bool:
    if not word:
        return True
    return all(c in _PUNCT_CHARS for c in word)


def _segment_utterances(words: list[dict], gap_threshold: float = 0.7) -> list[dict]:
    """Group words into utterances based on inter-word gaps.

    A gap > gap_threshold between consecutive words signals a new utterance/sentence.
    This is more accurate than VAD segments (which merge 10-30s blocks) because
    word timestamps give millisecond-level gap detection.

    Returns list of {start, end, word_start_idx, word_end_idx, first_word, last_word}
    """
    if not words:
        return []

    utterances = []
    utt_start_idx = 0
    utt_start = words[0]["start"]
    utt_end = words[0]["end"]

    for i in range(1, len(words)):
        w = words[i]
        gap = w["start"] - utt_end
        if gap > gap_threshold:
            utt_words = words[utt_start_idx:i]
            utterances.append({
                "start": round(utt_start, 3),
                "end": round(utt_end, 3),
                "word_start_idx": utt_start_idx,
                "word_end_idx": i,  # exclusive
                "first_word": utt_words[0]["word"] if utt_words else "",
                "last_word": utt_words[-1]["word"] if utt_words else "",
                "word_count": i - utt_start_idx,
            })
            utt_start_idx = i
            utt_start = w["start"]
            utt_end = w["end"]
        else:
            utt_end = max(utt_end, w["end"])

    # Final utterance
    utt_words = words[utt_start_idx:]
    utterances.append({
        "start": round(utt_start, 3),
        "end": round(utt_end, 3),
        "word_start_idx": utt_start_idx,
        "word_end_idx": len(words),
        "first_word": utt_words[0]["word"] if utt_words else "",
        "last_word": utt_words[-1]["word"] if utt_words else "",
        "word_count": len(words) - utt_start_idx,
    })
    return utterances


def _run_vad_segments(vad_path: str, audio_path: str) -> list[list[float]]:
    """Run fsmn-vad standalone to get speech segments.

    VAD model is 1.6MB, inference ~1-2s for 2-3min audio.
    Returns list of [start_s, end_s] in seconds (absolute file time).
    """
    from funasr import AutoModel
    vad_model = AutoModel(model=vad_path, disable_update=True, ncpu=4)
    res = vad_model.generate(input=audio_path, batch_size_s=60)
    if not res:
        return []
    val = res[0].get("value", [])
    segments = []
    for seg in val:
        if isinstance(seg, (list, tuple)) and len(seg) >= 2:
            segments.append([round(seg[0] / 1000.0, 3), round(seg[1] / 1000.0, 3)])
    return segments


def _merge_segments(segments: list[list[float]], merge_gap: float = 0.35) -> list[dict[str, float]]:
    """Merge segments with small gaps into speech intervals."""
    if not segments:
        return []
    sorted_segs = sorted(segments, key=lambda x: x[0])
    merged = []
    for s, e in sorted_segs:
        if merged and s - merged[-1]["end"] <= merge_gap:
            merged[-1]["end"] = round(max(merged[-1]["end"], e), 3)
        else:
            merged.append({"start": round(s, 3), "end": round(e, 3)})
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--sensevoice-model", default=None)
    ap.add_argument("--vad-model", default=None)
    ap.add_argument("--sample-rate", type=int, default=16000)
    ap.add_argument("--word-gap-threshold", type=float, default=0.7)
    ap.add_argument("--vad-merge-gap", type=float, default=0.35)
    ap.add_argument("--num-threads", type=int, default=4)
    args = ap.parse_args()

    source = Path(args.source)
    work_dir = Path(args.work_dir)
    out_path = Path(args.out)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Optimize CPU threading
    os.environ["OMP_NUM_THREADS"] = str(args.num_threads)
    os.environ["MKL_NUM_THREADS"] = str(args.num_threads)

    # Suppress FunASR verbose logging
    logging.getLogger("funasr").setLevel(logging.WARNING)
    logging.getLogger("modelscope").setLevel(logging.WARNING)

    # Check audio stream exists
    if not _has_audio(source):
        out_path.write_text(json.dumps({
            "status": "no_audio",
            "words": [],
            "utterances": [],
            "vad_segments": [],
            "speech_intervals": [],
            "bgm_detected": False,
            "emotion_tokens": [],
            "inference_time_s": 0,
        }))
        return

    # Extract 16kHz mono WAV
    audio_path = _extract_audio(source, work_dir, sr=args.sample_rate)

    # Resolve model paths
    base = args.model_dir or ""
    sv_path = args.sensevoice_model or (
        os.path.join(base, "SenseVoiceSmall") if base else _DEFAULT_SENSEVOICE
    )
    vad_path = args.vad_model or (
        os.path.join(base, "fsmn-vad") if base else _DEFAULT_VAD
    )

    import torch
    torch.set_num_threads(args.num_threads)

    from funasr import AutoModel

    # ══════════════════════════════════════════════════════════
    # Load SenseVoice (word timestamps) + fsmn-vad (internal chunking)
    # NO punc model — we only need word onset times, not punctuation.
    # ══════════════════════════════════════════════════════════
    t0 = time.time()
    model = AutoModel(
        model=sv_path,
        vad_model=vad_path,
        vad_kwargs={"max_single_segment_time": 30000},
        disable_update=True,
        ncpu=args.num_threads,
    )
    load_time = time.time() - t0

    # ══════════════════════════════════════════════════════════
    # Run ASR inference with word timestamps
    # ══════════════════════════════════════════════════════════
    t1 = time.time()
    results = model.generate(
        input=str(audio_path),
        batch_size_s=60,
        output_timestamp=True,
    )
    asr_time = time.time() - t1

    if not results:
        out_path.write_text(json.dumps({
            "status": "error",
            "words": [],
            "utterances": [],
            "vad_segments": [],
            "speech_intervals": [],
            "bgm_detected": False,
            "emotion_tokens": [],
            "inference_time_s": asr_time,
            "error": "No results from ASR",
        }))
        return

    # ══════════════════════════════════════════════════════════
    # Parse word timestamps
    # ══════════════════════════════════════════════════════════
    all_words: list[dict] = []
    bgm_detected = False
    emotion_tokens: list[str] = []

    _SPECIAL_TOKENS = {
        "<|BGM|>", "<|speech|>", "<|laughter|>", "<|cough|>", "<|sigh|>",
        "<|HAPPY|>", "<|SAD|>", "<|ANGRY|>", "<|NEUTRAL|>", "<|FEARFUL|>",
        "<|DISGUSTED|>", "<|SURPRISED|>", "<|woitn|>",
    }
    _EMOTION_TOKENS = {
        "<|HAPPY|>", "<|SAD|>", "<|ANGRY|>", "<|NEUTRAL|>", "<|FEARFUL|>",
        "<|DISGUSTED|>", "<|SURPRISED|>", "<|laughter|>", "<|sigh|>", "<|cough|>",
    }

    for r in results:
        text = r.get("text", "")
        timestamps = r.get("timestamp", [])
        words_list = r.get("words", [])

        # Detect special tokens from raw text
        for tok in _SPECIAL_TOKENS:
            if tok in text:
                if tok == "<|BGM|>":
                    bgm_detected = True
                elif tok in _EMOTION_TOKENS:
                    emotion_tokens.append(tok.strip("<>|"))

        if words_list and timestamps and len(words_list) == len(timestamps):
            for word, (s_ms, e_ms) in zip(words_list, timestamps):
                word_clean = str(word).strip()
                if _is_punctuation(word_clean):
                    continue
                if word_clean.startswith("<|") and word_clean.endswith("|>"):
                    continue
                s_s = s_ms / 1000.0
                e_s = e_ms / 1000.0
                if s_s < 0 or e_s < 0 or e_s < s_s:
                    continue
                all_words.append({
                    "word": word_clean,
                    "start": round(s_s, 3),
                    "end": round(e_s, 3),
                })

    all_words.sort(key=lambda w: w["start"])

    # ══════════════════════════════════════════════════════════
    # Segment into utterances from word gaps
    # ══════════════════════════════════════════════════════════
    utterances = _segment_utterances(all_words, gap_threshold=args.word_gap_threshold)

    # ══════════════════════════════════════════════════════════
    # Run standalone VAD for Tier-2 fallback (non-verbal sounds)
    # ══════════════════════════════════════════════════════════
    t2 = time.time()
    vad_segments_raw = _run_vad_segments(vad_path, str(audio_path))
    vad_time = time.time() - t2

    speech_intervals = _merge_segments(vad_segments_raw, merge_gap=args.vad_merge_gap)
    infer_time = asr_time + vad_time

    result = {
        "status": "ready",
        "words": all_words,
        "utterances": utterances,
        "vad_segments": vad_segments_raw,
        "speech_intervals": speech_intervals,
        "bgm_detected": bgm_detected,
        "emotion_tokens": emotion_tokens,
        "inference_time_s": round(infer_time, 2),
        "asr_time_s": round(asr_time, 2),
        "vad_time_s": round(vad_time, 2),
        "load_time_s": round(load_time, 2),
        "model": "SenseVoiceSmall+fsmn-vad",
        "config": {
            "word_gap_threshold": args.word_gap_threshold,
            "vad_merge_gap": args.vad_merge_gap,
            "sample_rate": args.sample_rate,
            "num_threads": args.num_threads,
        },
    }

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        f"ASR anchor complete: {len(all_words)} words, {len(utterances)} utterances, "
        f"{len(vad_segments_raw)} VAD segments, "
        f"asr={asr_time:.1f}s vad={vad_time:.1f}s bgm={bgm_detected}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
