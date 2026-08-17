#!/usr/bin/env python3
"""Test VAD on ep07 with new per-track thresholds + extend_window=1.5.
Reuses cached demucs output, only runs Silero VAD + merge.
"""
import json, sys, os
sys.path.insert(0, "/Users/liuzx/Code/python/work_ai/auto_cut_bot/packages/autocut-core")

# Paths
EP07 = "/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/when-lucifer-kneels/videos/ep07.mp4"
CACHED_WORK = "/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/when-lucifer-kneels/vad_cache/_work/fc/fc84a1ea257d47169da648100a2ce95d83bcd2005a062427af7f4badb3b150d4"
VOCALS = f"{CACHED_WORK}/demucs/htdemucs/mix/vocals.wav"
MIX = f"{CACHED_WORK}/mix.wav"

# New per-track thresholds
THRESHOLD_DEMUCS = 0.25
THRESHOLD_ORIGINAL = 0.45
THRESHOLD_NO_VOCALS = 0.55

import torch
from pathlib import Path
from silero_vad import get_speech_timestamps, load_silero_vad

mix_path = MIX if Path(MIX).is_file() else "/tmp/vad-test-ep07/mix.wav"
vocals_path = VOCALS

print(f"Mix: {mix_path}")
print(f"Vocals: {vocals_path}")
assert Path(vocals_path).is_file(), f"vocals.wav not found: {vocals_path}"
assert Path(mix_path).is_file(), f"mix.wav not found: {mix_path}"

print("Loading Silero VAD model...")
model = load_silero_vad()

def read_audio(path, sr=16000):
    import subprocess
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
        check=True, capture_output=True,
    )
    return torch.frombuffer(bytearray(result.stdout), dtype=torch.float32).clone()

def detect(audio_path, threshold, label=""):
    wav = read_audio(audio_path)
    raw = get_speech_timestamps(
        wav, model, sampling_rate=16000, threshold=threshold,
        min_speech_duration_ms=100, min_silence_duration_ms=350,
        speech_pad_ms=120, return_seconds=True,
    )
    ivs = [{"start": float(t["start"]), "end": float(t["end"])} for t in raw]
    print(f"  {label}: {len(ivs)} intervals, threshold={threshold}")
    return ivs

print("\n--- VAD Detection (new per-track thresholds) ---")
original_ivs = detect(mix_path, THRESHOLD_ORIGINAL, "original_mix")
demucs_ivs = detect(vocals_path, THRESHOLD_DEMUCS, "demucs_vocals")

no_vocals_path = Path(vocals_path).parent / "no_vocals.wav"
no_vocals_ivs = []
if no_vocals_path.is_file():
    no_vocals_ivs = detect(str(no_vocals_path), THRESHOLD_NO_VOCALS, "no_vocals")

from dataclasses import dataclass

@dataclass
class SpeechInterval:
    start: float
    end: float
    track: str = "union"
    @property
    def duration(self): return self.end - self.start

def smart_merge(demucs, original, no_vocals=None, extend_window=1.5, min_gap=0.15, phrase_gap=0.15):
    if not demucs:
        all_ivs = sorted(list(original) + list(no_vocals or []), key=lambda x: x.start)
        return all_ivs  # simplified
    supplementary = list(original) + list(no_vocals or [])
    sorted_d = sorted(demucs, key=lambda x: x.start)
    d_phrases = [[sorted_d[0]]]
    for iv in sorted_d[1:]:
        if iv.start - d_phrases[-1][-1].end <= phrase_gap:
            d_phrases[-1].append(iv)
        else:
            d_phrases.append([iv])
    d_spans = [(ph[0].start, max(iv.end for iv in ph)) for ph in d_phrases]
    sorted_s = sorted(supplementary, key=lambda x: x.start)
    s_phrases = []
    if sorted_s:
        cur = [sorted_s[0]]
        for iv in sorted_s[1:]:
            if iv.start - cur[-1].end <= phrase_gap:
                cur.append(iv)
            else:
                s_phrases.append(cur); cur = [iv]
        s_phrases.append(cur)
    s_spans = [(ph[0].start, max(iv.end for iv in ph)) for ph in s_phrases]
    SAFETY_MARGIN = min_gap
    result_phrases = []
    for pi, (d_start, d_end) in enumerate(d_spans):
        p_start, p_end = d_start, d_end
        prev_cap = d_spans[pi-1][1] + SAFETY_MARGIN if pi > 0 else 0.0
        next_cap = d_spans[pi+1][0] - SAFETY_MARGIN if pi < len(d_spans)-1 else float('inf')
        for s_s, s_e in s_spans:
            if s_e > p_start and s_s < p_end:
                if s_s < p_start and s_s >= prev_cap and (p_start - s_s) <= extend_window:
                    p_start = s_s
                if s_e > p_end and s_e <= next_cap and (s_e - p_end) <= extend_window:
                    p_end = s_e
            elif s_e >= p_start - extend_window and s_s < p_start and s_e > prev_cap:
                if s_s >= prev_cap and (p_start - s_s) <= extend_window:
                    p_start = s_s
            elif s_s <= p_end + extend_window and s_e > p_end and s_s < next_cap:
                if s_e <= next_cap and (s_e - p_end) <= extend_window:
                    p_end = s_e
        result_phrases.append(SpeechInterval(start=p_start, end=p_end, track="union"))
    result_phrases.sort(key=lambda x: x.start)
    merged = []
    for ph in result_phrases:
        if merged and ph.start - merged[-1].end <= min_gap:
            merged[-1].end = max(merged[-1].end, ph.end)
        else:
            merged.append(ph)
    return merged

def to_si(ivs, track):
    return [SpeechInterval(start=i["start"], end=i["end"], track=track) for i in ivs]

demucs_si = to_si(demucs_ivs, "demucs_vocals")
original_si = to_si(original_ivs, "original_mix")
no_vocals_si = to_si(no_vocals_ivs, "no_vocals") if no_vocals_ivs else None

print(f"\n--- Smart Merge (extend_window=1.5) ---")
merged = smart_merge(
    demucs_si, original_si, no_vocals_si,
    extend_window=1.5, min_gap=0.15, phrase_gap=0.15,
)
print(f"Merged: {len(merged)} intervals")

print(f"\n--- Analysis around ep07@79.4s (the 'Go' area) ---")
print(f"\nDemucs intervals near 74-90s:")
for iv in demucs_si:
    if 74 <= iv.start <= 90 or 74 <= iv.end <= 90:
        print(f"  [{iv.start:.1f} - {iv.end:.1f}] (dur={iv.end-iv.start:.1f}s)")

print(f"\nOriginal_mix intervals near 74-90s:")
for iv in original_si:
    if 74 <= iv.start <= 90 or 74 <= iv.end <= 90:
        print(f"  [{iv.start:.1f} - {iv.end:.1f}] (dur={iv.end-iv.start:.1f}s)")

print(f"\nMerged intervals near 74-92s:")
for iv in merged:
    if 74 <= iv.start <= 92 or 74 <= iv.end <= 92:
        print(f"  [{iv.start:.1f} - {iv.end:.1f}] (dur={iv.end-iv.start:.1f}s)")

long_segs = [iv for iv in merged if iv.duration > 10]
print(f"\n--- Long segments (>10s): {len(long_segs)} ---")
for iv in long_segs:
    print(f"  [{iv.start:.1f} - {iv.end:.1f}] dur={iv.duration:.1f}s")

durs = [iv.duration for iv in merged]
print(f"\n--- Summary ---")
print(f"Total merged intervals: {len(merged)}")
print(f"Avg duration: {sum(durs)/len(durs):.2f}s" if durs else "N/A")
print(f"Max duration: {max(durs):.2f}s" if durs else "N/A")
print(f">10s segments: {len(long_segs)}")

near_79 = [iv for iv in merged if iv.start <= 80.0 and iv.end >= 79.0]
print(f"\nMerged intervals covering ~79.4s:")
for iv in near_79:
    captured = "CAPTURED ✅" if iv.start <= 79.6 else "MISSED ❌"
    print(f"  [{iv.start:.1f} - {iv.end:.1f}] ← 'Go' at 79.4s: {captured}")
