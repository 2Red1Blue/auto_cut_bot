#!/usr/bin/env python3
"""VAD worker: runs inside .venv-audio-boundary, produces speech_intervals JSON.

This script is invoked via subprocess from the main pipeline process.
It uses demucs for source separation and silero-vad for speech detection.

Usage:
    python vad_worker.py --source <video> --work-dir <dir> --device <cpu|mps|cuda> --out <json>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _ffmpeg() -> str:
    for candidate in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        try:
            subprocess.run([candidate, "-version"], capture_output=True, check=True)
            return candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise RuntimeError("ffmpeg not found")


def _ffprobe(ffmpeg: str) -> str:
    return ffmpeg.replace("ffmpeg", "ffprobe")


def read_audio_mono(path: str, sampling_rate: int, ffmpeg: str):
    """Decode audio to float32 mono PCM at target sample rate."""
    import torch
    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", path, "-vn", "-ac", "1", "-ar", str(sampling_rate),
            "-f", "f32le", "pipe:1",
        ],
        check=True, capture_output=True,
    )
    if not result.stdout:
        raise ValueError(f"decoded audio is empty: {path}")
    return torch.frombuffer(bytearray(result.stdout), dtype=torch.float32).clone()


def merge_intervals(intervals: list[dict], min_gap: float) -> list[dict]:
    """Merge overlapping or close intervals."""
    if not intervals:
        return []
    merged = []
    for item in sorted(intervals, key=lambda v: v["start"]):
        cur = {"start": float(item["start"]), "end": float(item["end"])}
        if not merged or cur["start"] - merged[-1]["end"] >= min_gap:
            merged.append(cur)
        else:
            merged[-1]["end"] = max(merged[-1]["end"], cur["end"])
    return merged


def detect_track(audio_path: Path, *, ffmpeg: str, model, get_ts, sr: int,
                 threshold: float, min_speech_ms: int, min_silence_ms: int,
                 speech_pad_ms: int) -> list[dict]:
    """Run VAD on a single audio track, return [{start, end}, ...]."""
    wav = read_audio_mono(str(audio_path), sampling_rate=sr, ffmpeg=ffmpeg)
    raw = get_ts(
        wav, model,
        sampling_rate=sr,
        threshold=threshold,
        min_speech_duration_ms=min_speech_ms,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
        return_seconds=True,
    )
    return [{"start": float(t["start"]), "end": float(t["end"])} for t in raw]


def run_demucs(mix_path: Path, work_dir: Path, *, device: str, model_name: str = "htdemucs") -> Path | None:
    """Run demucs separation, return vocals.wav path or None on failure."""
    demucs_out = work_dir / "demucs"
    vocals_path = demucs_out / model_name / mix_path.stem / "vocals.wav"
    if vocals_path.is_file():
        return vocals_path

    demucs_cli = shutil.which("demucs") or str(Path(sys.executable).parent / "demucs")
    if not Path(demucs_cli).exists():
        print(f"WARNING: demucs CLI not found at {demucs_cli}", file=sys.stderr)
        return None

    # Clean stale outputs
    if demucs_out.exists():
        shutil.rmtree(demucs_out, ignore_errors=True)

    print(f"Running demucs ({model_name}, {device})...", file=sys.stderr)
    try:
        subprocess.run(
            [
                demucs_cli,
                "--two-stems", "vocals",
                "-n", model_name,
                "-d", device,
                "-o", str(demucs_out),
                str(mix_path),
            ],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"WARNING: demucs failed: {e.stderr[-500:]}", file=sys.stderr)
        return None

    if vocals_path.is_file():
        return vocals_path
    # Search for any vocals file
    for p in demucs_out.rglob("vocals.wav"):
        return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="Source video path")
    ap.add_argument("--work-dir", required=True, help="Working directory for intermediates")
    ap.add_argument("--device", default="cpu", help="torch device (cpu|mps|cuda)")
    ap.add_argument("--out", required=True, help="Output JSON path")
    ap.add_argument("--cache-dir", default=None, help="Torch/Silero model cache directory")
    # VAD parameters
    ap.add_argument("--demucs-model", default="htdemucs")
    ap.add_argument("--sample-rate", type=int, default=16000)
    ap.add_argument("--vad-threshold-demucs", type=float, default=0.25,
                     help="VAD threshold for demucs_vocals track (clean, low)")
    ap.add_argument("--vad-threshold-original", type=float, default=0.45,
                     help="VAD threshold for original_mix track (has BGM, high)")
    ap.add_argument("--vad-threshold-no-vocals", type=float, default=0.55,
                     help="VAD threshold for no_vocals track (accompaniment, highest)")
    ap.add_argument("--min-speech-ms", type=int, default=100)
    ap.add_argument("--min-silence-ms", type=int, default=350)
    ap.add_argument("--speech-pad-ms", type=int, default=120)
    ap.add_argument("--min-safe-gap", type=float, default=0.35)
    ap.add_argument("--skip-demucs", action="store_true", help="Skip source separation (mix only)")
    args = ap.parse_args()

    # Setup cache directory for torch hub / silero model
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir) if args.cache_dir else work_dir / "model_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TORCH_HOME"] = str(cache_dir / "torch")
    os.environ["HF_HOME"] = str(cache_dir / "huggingface")
    # Do NOT override HOME - pre-downloaded models in ~/.cache must remain accessible
    # Silero will use cache_dir / ".cache" via TORCH_HOME/HF_HOME

    ffmpeg = _ffmpeg()
    ffprobe = _ffprobe(ffmpeg)
    source_path = Path(args.source)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if source has audio
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "json", str(source_path)],
        capture_output=True, text=True,
    )
    has_audio = bool(json.loads(probe.stdout or "{}").get("streams"))
    if not has_audio:
        out_path.write_text(json.dumps({
            "status": "no_audio",
            "speech_intervals": [],
            "track_intervals": {"original_mix": [], "demucs_vocals": []},
            "demucs_used": False,
        }))
        print("No audio track found.", file=sys.stderr)
        return

    # Step 1: Extract stereo mix for demucs
    mix_path = work_dir / "mix.wav"
    if not mix_path.is_file():
        print("Extracting audio...", file=sys.stderr)
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source_path),
                "-map", "0:a:0",
                "-ac", "2", "-ar", "44100",
                "-c:a", "pcm_s16le",
                str(mix_path),
            ],
            check=True, capture_output=True, text=True,
        )

    # Step 2: Demucs source separation
    vocals_path = None
    demucs_used = False
    if not args.skip_demucs:
        vocals_path = run_demucs(mix_path, work_dir, device=args.device, model_name=args.demucs_model)
        demucs_used = vocals_path is not None

    # Step 3: Load Silero VAD
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    print("Loading Silero VAD model...", file=sys.stderr)
    vad_model = load_silero_vad()

    # Step 4: Run VAD on tracks
    track_intervals = {}

    # Original mix (resampled to mono 16kHz by ffmpeg in detect_track)
    print("Detecting speech on original mix...", file=sys.stderr)
    track_intervals["original_mix"] = detect_track(
        mix_path, ffmpeg=ffmpeg, model=vad_model, get_ts=get_speech_timestamps,
        sr=args.sample_rate, threshold=args.vad_threshold_original,
        min_speech_ms=args.min_speech_ms, min_silence_ms=args.min_silence_ms,
        speech_pad_ms=args.speech_pad_ms,
    )

    # Demucs vocals track
    if vocals_path is not None:
        print("Detecting speech on demucs vocals...", file=sys.stderr)
        track_intervals["demucs_vocals"] = detect_track(
            vocals_path, ffmpeg=ffmpeg, model=vad_model, get_ts=get_speech_timestamps,
            sr=args.sample_rate, threshold=args.vad_threshold_demucs,
            min_speech_ms=args.min_speech_ms, min_silence_ms=args.min_silence_ms,
            speech_pad_ms=args.speech_pad_ms,
        )
        # Also detect on no_vocals track (catches shouted/screamed speech
        # that Demucs routes to accompaniment, plus speech mixed with loud SFX)
        no_vocals_path = vocals_path.parent / "no_vocals.wav"
        if no_vocals_path.is_file():
            print("Detecting speech on no_vocals (accompaniment)...", file=sys.stderr)
            # Use lower threshold for no_vocals since it has more noise
            track_intervals["no_vocals"] = detect_track(
                no_vocals_path, ffmpeg=ffmpeg, model=vad_model, get_ts=get_speech_timestamps,
                sr=args.sample_rate, threshold=args.vad_threshold_no_vocals,
                min_speech_ms=args.min_speech_ms, min_silence_ms=args.min_silence_ms,
                speech_pad_ms=args.speech_pad_ms,
            )
        else:
            track_intervals["no_vocals"] = []
    else:
        track_intervals["demucs_vocals"] = []
        track_intervals["no_vocals"] = []

    # Step 5: Union merge
    all_ivs = []
    for ivs in track_intervals.values():
        all_ivs.extend(ivs)
    union = merge_intervals(all_ivs, args.min_safe_gap)

    result = {
        "status": "ready",
        "speech_intervals": union,
        "track_intervals": track_intervals,
        "demucs_used": demucs_used,
        "config": {
            "demucs_model": args.demucs_model,
            "sample_rate": args.sample_rate,
            "vad_threshold_demucs": args.vad_threshold_demucs,
            "vad_threshold_original": args.vad_threshold_original,
            "vad_threshold_no_vocals": args.vad_threshold_no_vocals,
            "min_speech_ms": args.min_speech_ms,
            "min_silence_ms": args.min_silence_ms,
            "speech_pad_ms": args.speech_pad_ms,
            "min_safe_gap": args.min_safe_gap,
            "device": args.device,
        },
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"VAD complete: {len(union)} speech intervals, demucs={'yes' if demucs_used else 'no'}", file=sys.stderr)


if __name__ == "__main__":
    main()
