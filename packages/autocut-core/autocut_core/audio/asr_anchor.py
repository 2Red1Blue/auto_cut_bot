"""ASR-based Audio Anchor Detector — SenseVoice + fsmn-vad in a single pass.

Architecture (three-tier cascade / 三层确定性漏斗):
  Tier 1: Word-level onset from SenseVoice ASR (precision ~30-50ms)
          → Uses utterance segmentation (word gaps >0.7s) to find sentence starts
          → This is the PRIMARY anchor; ASR timestamps are semantically filtered
            and don't false-trigger on BGM/drums.
  Tier 2: VAD segment onset from fsmn-vad
          → Fallback for non-verbal vocalizations: screams, gasps, whispers,
            loud BGM-swallowed sounds where ASR produces no words.
  Tier 3: Pure visual (PySceneDetect) when no audio detected.

Why this replaces Demucs+Silero:
  - ~100x faster: ~25s CPU per 2min episode vs 30-50min for Demucs separation
  - Catches quiet speech (breathy "Go" at -30dB) that Demucs completely misses
  - Semantically filtered: ASR timestamps don't false-trigger on BGM/drums
  - SenseVoice detects <|BGM|>/<|ANGRY|>/<|laughter|> etc. natively

ASR is used ONLY for onset/offset timestamp detection — NOT for transcription/subtitles.
The word text content is used only for optional cue_text fuzzy matching.

Crossfade/transition policy:
  - The snap function returns a `needs_fade` hint when the cut might clip a sound
  - Default micro-crossfade: 50-100ms equal-power fade to mask click/pop sounds
  - J-cut/L-cut/B-roll are agent-invoked tools (NOT automatic fallbacks here)
  - Transitions (cross-dissolve/flash/blur) are render-stage concerns

The detector runs in a subprocess using the .venv-audio-boundary venv,
same as the old DemucsSileroDetector, to avoid torch import conflicts
in the main pipeline process.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from autocut_core.audio.vad import SpeechDetector, VADResult, SpeechInterval
from autocut_core.contracts.audio_boundary import AudioBoundaryPolicy


# ---------------------------------------------------------------------------
# Data types (rich anchor data beyond basic VADResult)
# ---------------------------------------------------------------------------

@dataclass
class WordTimestamp:
    """A single word with its precise onset/offset in seconds."""
    word: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class UtteranceBoundary:
    """A contiguous speech utterance segmented by inter-word gaps."""
    start: float
    end: float
    word_start_idx: int
    word_end_idx: int
    first_word: str
    last_word: str
    word_count: int

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class AudioAnchorResult(VADResult):
    """Extended VADResult with word/utterance data for three-tier snap.

    Inherits speech_intervals/is_in_speech() from VADResult for backward compat,
    and adds words/utterances/vad_segments for precise ASR-anchored snap.
    """
    words: list[WordTimestamp] = field(default_factory=list)
    utterances: list[UtteranceBoundary] = field(default_factory=list)
    vad_segments: list[tuple[float, float]] = field(default_factory=list)
    bgm_detected: bool = False
    emotion_tokens: list[str] = field(default_factory=list)
    inference_time_s: float = 0.0
    asr_time_s: float = 0.0
    vad_time_s: float = 0.0

    def find_containing_utterance(self, t: float) -> UtteranceBoundary | None:
        for u in self.utterances:
            if u.start <= t <= u.end:
                return u
        return None

    def find_nearest_utterance(
        self, t: float, search_radius: float = 2.0, direction: str = "both",
    ) -> tuple[UtteranceBoundary | None, float]:
        best_utt = None
        best_dist = float("inf")
        for u in self.utterances:
            if direction == "after":
                if u.start >= t - 0.1:
                    dist = abs(u.start - t)
                    if dist <= search_radius and dist < best_dist:
                        best_dist = dist
                        best_utt = u
            elif direction == "before":
                # For end cuts: find nearest utterance end
                dist = abs(u.end - t)
                if dist <= search_radius and dist < best_dist:
                    best_dist = dist
                    best_utt = u
            else:
                dist = abs(u.start - t)
                if dist <= search_radius and dist < best_dist:
                    best_dist = dist
                    best_utt = u
        return best_utt, (best_dist if best_utt else float("inf"))

    def find_vad_onset_near(self, t: float, search_radius: float = 3.0) -> float | None:
        best, best_d = None, float("inf")
        for s, e in self.vad_segments:
            if s <= t + search_radius and e >= t - 0.5:
                d = abs(s - t)
                if d < best_d:
                    best_d, best = d, s
        return best

    def find_vad_offset_near(self, t: float, search_radius: float = 3.0) -> float | None:
        """Find nearest VAD segment END near timestamp t.

        For end-snap safety: only returns segments whose end is within search_radius of t,
        AND requires the segment to overlap or be immediately adjacent to t
        (prevents snapping back to speech that ended long before VLM end point,
        which would clip action/silence the VLM intentionally included).
        """
        best, best_d = None, float("inf")
        for s, e in self.vad_segments:
            # Segment must end within search_radius of t, and start not too far after t
            if e >= t - search_radius and s <= t + 0.5:
                d = abs(e - t)
                if d <= search_radius and d < best_d:
                    best_d, best = d, e
        return best


# ---------------------------------------------------------------------------
# ASR Anchor Detector
# ---------------------------------------------------------------------------

class ASRAnchorDetector:
    """SenseVoice + fsmn-vad based audio anchor detector.

    Returns AudioAnchorResult (which extends VADResult for backward compatibility).
    Runs in a subprocess using .venv-audio-boundary to isolate torch imports.
    """

    def __init__(
        self,
        vad_python: Path | None = None,
        cache_dir: Path | None = None,
        device: str = "cpu",
        policy: AudioBoundaryPolicy | None = None,
        word_gap_threshold: float = 0.7,
        vad_merge_gap: float = 0.35,
        num_threads: int = 4,
    ):
        self.device = device
        self.word_gap_threshold = word_gap_threshold
        self.vad_merge_gap = vad_merge_gap
        self.num_threads = num_threads

        # Find VAD venv python
        self._python = None
        if vad_python and vad_python.is_file():
            self._python = str(vad_python)
        else:
            import autocut_core
            pkg_root = Path(autocut_core.__file__).parent.parent.parent.parent
            candidates = [
                pkg_root / ".venv-audio-boundary" / "bin" / "python",
                Path.cwd() / ".venv-audio-boundary" / "bin" / "python",
            ]
            for c in candidates:
                if c.is_file():
                    self._python = str(c)
                    break

        self._cache_dir = Path(cache_dir) if cache_dir else None

    def detect(self, source_path: Path, *, force: bool = False) -> AudioAnchorResult:
        source_path = Path(source_path)
        try:
            h = hashlib.sha256()
            with open(source_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            sha = h.hexdigest()[:16]
        except Exception:
            sha = str(source_path).encode().hex()[:16]

        cache_path = None
        if self._cache_dir:
            cache_path = self._cache_dir / f"asr_anchor_{sha}.json"
            if not force and cache_path.is_file():
                try:
                    return _result_from_dict(json.loads(cache_path.read_text()))
                except Exception:
                    pass

        if self._python is None:
            return AudioAnchorResult(
                source_path=str(source_path), source_sha256=sha,
                status="error", error="ASR venv python not found",
            )

        work_dir = Path(tempfile.mkdtemp(prefix="asr_anchor_"))
        out_json = work_dir / "result.json"

        try:
            worker = Path(__file__).parent / "asr_anchor_worker.py"
            cmd = [
                self._python, str(worker),
                "--source", str(source_path),
                "--work-dir", str(work_dir),
                "--out", str(out_json),
                "--device", self.device,
                "--word-gap-threshold", str(self.word_gap_threshold),
                "--vad-merge-gap", str(self.vad_merge_gap),
                "--num-threads", str(self.num_threads),
            ]
            env = os.environ.copy()
            env["HF_HUB_OFFLINE"] = "1"
            env["MODELSCOPE_OFFLINE"] = "1"
            env["OMP_NUM_THREADS"] = str(self.num_threads)
            env["MKL_NUM_THREADS"] = str(self.num_threads)

            subprocess.run(cmd, check=True, capture_output=True, text=True,
                           timeout=600, env=env)

            if out_json.is_file():
                data = json.loads(out_json.read_text())
                result = _result_from_dict(data)
                result.source_path = str(source_path)
                result.source_sha256 = sha
                result.demucs_used = False
                if cache_path:
                    _atomic_write_json(cache_path, _result_to_dict(result))
                return result
            else:
                return AudioAnchorResult(
                    source_path=str(source_path), source_sha256=sha,
                    status="error", error="Worker produced no output",
                )
        except subprocess.CalledProcessError as e:
            return AudioAnchorResult(
                source_path=str(source_path), source_sha256=sha,
                status="error", error=f"Worker failed: {e.stderr[-500:]}",
            )
        except subprocess.TimeoutExpired:
            return AudioAnchorResult(
                source_path=str(source_path), source_sha256=sha,
                status="error", error="Worker timed out (600s)",
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def detect_batch(self, source_paths, *, force=False):
        return {str(p): self.detect(p, force=force) for p in source_paths}


# ---------------------------------------------------------------------------
# Three-tier cascade snap logic
# ---------------------------------------------------------------------------

def three_tier_snap_start(
    vlm_timestamp: float,
    visual_cuts: list[float],
    anchor_result: VADResult | AudioAnchorResult | None,
    *,
    lead_in_audio: float = 0.15,
    lead_in_visual: float = 0.05,
    visual_lead_window: float = 0.60,
    visual_follow_window: float = 0.45,
    search_radius: float = 2.0,
    max_shift: float = 5.0,
    cue_text: str | None = None,
    micro_crossfade_ms: float = 80.0,
) -> dict[str, Any]:
    """Three-tier deterministic cascade for highlight START alignment.

    Tier 1: ASR utterance onset (if anchor_result has words/utterances)
      a) VLM inside utterance → snap to first word of that utterance
      b) VLM between utterances → snap to next utterance start if within search_radius
      c) cue_text → fuzzy match to find exact target word → snap to containing utterance
    Tier 2: VAD segment onset (non-verbal fallback: screams, gasps, BGM-swallowed sounds)
    Tier 3: Pure visual snap to nearest PySceneDetect cut

    After selecting T_audio, align with visual cuts:
    - Visual cut within [T_audio - visual_lead_window, T_audio + visual_follow_window]
      → snap to cut + lead_in_visual (cut at visual boundary)
    - Otherwise → snap to T_audio - lead_in_audio (protect audio onset)

    Returns dict with: final_start, decision, anchor_source, audio_onset,
                       needs_fade, fade_ms, info
    """
    t_audio = None
    anchor_source = "none"
    info = ""

    is_anchor = isinstance(anchor_result, AudioAnchorResult)

    # ── Tier 1: ASR utterance onset ──
    if is_anchor and anchor_result.status == "ready" and anchor_result.utterances:

        # 1a) cue_text fuzzy match
        if cue_text and anchor_result.words:
            cue_lower = cue_text.lower().strip()
            best_word = None
            best_dist = float("inf")
            for w in anchor_result.words:
                wl = w.word.lower().strip()
                if not wl or len(wl) < 2:
                    continue
                if wl in cue_lower or cue_lower.startswith(wl[:3]) or wl.startswith(cue_lower[:3]):
                    d = abs(w.start - vlm_timestamp)
                    if d <= search_radius + 1.0 and d < best_dist:
                        best_dist, best_word = d, w
            if best_word:
                containing = anchor_result.find_containing_utterance(best_word.start)
                if containing:
                    t_audio = containing.start
                    anchor_source = "asr_cue"
                    info = f"Cue '{best_word.word}' in utt starting at {containing.start:.2f}s"
                else:
                    t_audio = best_word.start
                    anchor_source = "asr_cue"
                    info = f"Cue word '{best_word.word}' at {best_word.start:.2f}s"

        # 1b) VLM inside utterance
        if t_audio is None:
            containing = anchor_result.find_containing_utterance(vlm_timestamp)
            if containing:
                t_audio = containing.start
                anchor_source = "asr_utt_containing"
                info = f"VLM in utt[{containing.start:.2f}-{containing.end:.2f}] '{containing.first_word}'"

        # 1c) VLM in gap: find next utterance
        if t_audio is None:
            u, d = anchor_result.find_nearest_utterance(vlm_timestamp, search_radius, "after")
            if u is None:
                u, d = anchor_result.find_nearest_utterance(vlm_timestamp, search_radius, "both")
            if u and d <= search_radius:
                # Guard: if VLM start is in pre-speech silence and speech starts > 1.5s after,
                # prefer visual snap (snapping forward too much adds unrelated lead-in)
                pre_speech_gap = u.start - vlm_timestamp
                if pre_speech_gap <= 1.5:
                    t_audio = u.start
                    anchor_source = "asr_utt_nearest"
                    info = f"Nearest utt '{u.first_word}' at {u.start:.2f}s (d={d:.2f}s)"
                else:
                    info = f"Utt '{u.first_word}' starts at {u.start:.2f}s but {pre_speech_gap:.2f}s after VLM, using visual"

    # ── Tier 2: VAD segment onset ──
    if t_audio is None and is_anchor and anchor_result.status == "ready":
        v = anchor_result.find_vad_onset_near(vlm_timestamp, search_radius + 1.0)
        if v is not None and abs(v - vlm_timestamp) <= max_shift:
            # Guard: reject VAD onsets too far after VLM (pre-speech silence)
            pre_vad_gap = v - vlm_timestamp
            if pre_vad_gap <= 2.0:
                t_audio = v
                anchor_source = "vad_fallback"
                info = f"VAD onset at {v:.2f}s (non-verbal or ASR-missed)"
            else:
                info = f"VAD onset at {v:.2f}s is {pre_vad_gap:.2f}s after VLM, using visual"

    # Fallback: if no rich anchor, try basic VAD is_in_speech for safety
    if t_audio is None and anchor_result and not is_anchor:
        containing = anchor_result.find_containing_interval(vlm_timestamp)
        if containing:
            t_audio = containing.start
            anchor_source = "vad_interval"
            info = f"In speech interval starting at {containing.start:.2f}s"

    # ── Tier 3: Final alignment ──
    needs_fade = False
    fade_ms = 0.0

    if t_audio is not None and abs(t_audio - vlm_timestamp) <= max_shift:
        # ── Visual alignment priority ──
        # Priority 1: Cuts BEFORE audio onset (camera leads speaker → ideal, no clipping)
        #   Search [t_audio - visual_lead_window, t_audio] for the latest cut before audio
        lead_cuts = [c for c in visual_cuts if (t_audio - visual_lead_window) <= c <= t_audio]
        # Priority 2: Cuts VERY shortly AFTER audio onset (tight follow, < lead_in_audio)
        #   These are acceptable because they only clip <0.15s of the onset
        tight_follow_cuts = [c for c in visual_cuts if t_audio < c <= t_audio + lead_in_audio + 0.05]
        # Priority 3: Wider follow cuts (up to visual_follow_window) — only use if NO lead cut exists
        #   These clip more of the word, but are still better than a jarring mid-shot cut
        wide_follow_cuts = [c for c in visual_cuts if t_audio + lead_in_audio + 0.05 < c <= t_audio + visual_follow_window]

        chosen_cut = None
        chosen_was_follow = False

        if lead_cuts:
            # Pick the latest (closest to audio onset) lead cut
            chosen_cut = max(lead_cuts)  # closest before t_audio
            chosen_was_follow = False
        elif tight_follow_cuts:
            # Tight follow: pick earliest cut after audio (minimizes clipping)
            chosen_cut = min(tight_follow_cuts)
            chosen_was_follow = True
        elif wide_follow_cuts:
            # Wide follow: only acceptable if cut is within ~0.3s of audio
            # (camera cuts to speaker right as they start — dialogue editing pattern)
            best_wide = min(wide_follow_cuts)  # earliest after audio
            if best_wide - t_audio <= 0.30:
                chosen_cut = best_wide
                chosen_was_follow = True
            # else: don't use follow cuts that clip >0.3s of audio — fall through to audio_onset_lead

        if chosen_cut is not None:
            final_point = chosen_cut + lead_in_visual
            decision = "visual_cut_aligned"
            gap = abs(t_audio - chosen_cut)
            if chosen_was_follow:
                # Cutting after audio onset → clip a small bit, need fade
                needs_fade = True
                fade_ms = micro_crossfade_ms
            else:
                # Cutting before audio onset → no clipping
                # Fade only when cut is EXTREMELY close to audio (<0.05s gap = almost touching)
                # This prevents audible clicks when cut is right at the onset boundary
                needs_fade = gap < 0.05
                fade_ms = micro_crossfade_ms if needs_fade else 0.0
        else:
            final_point = max(0.0, t_audio - lead_in_audio)
            decision = "audio_onset_lead"
            needs_fade = True
            fade_ms = micro_crossfade_ms
    else:
        # No usable audio anchor → pure visual
        if visual_cuts:
            closest = min(visual_cuts, key=lambda c: abs(c - vlm_timestamp))
            final_point = closest + lead_in_visual
        else:
            final_point = vlm_timestamp
        decision = "pure_visual_cut"
        anchor_source = "visual_only"
        if t_audio is not None and abs(t_audio - vlm_timestamp) > max_shift:
            info = f"Audio at {t_audio:.2f}s >{max_shift}s from VLM, using visual"
            t_audio = None
        elif not info:
            info = "No audio anchor (silent/action scene)"
        needs_fade = False

    # Safety clamp
    if abs(final_point - vlm_timestamp) > max_shift:
        final_point = vlm_timestamp
        decision = "vlm_fallback"
        needs_fade = True
        fade_ms = micro_crossfade_ms

    return {
        "final_start": round(final_point, 3),
        "decision": decision,
        "anchor_source": anchor_source,
        "audio_onset": round(t_audio, 3) if t_audio is not None else None,
        "needs_fade": needs_fade,
        "fade_ms": round(fade_ms, 1) if needs_fade else 0.0,
        "info": info,
    }


def three_tier_snap_end(
    vlm_timestamp: float,
    visual_cuts: list[float],
    anchor_result: VADResult | AudioAnchorResult | None,
    *,
    lead_out_audio: float = 0.10,
    lead_out_visual: float = 0.05,
    visual_tail_window: float = 0.60,
    search_radius: float = 2.0,
    max_shift: float = 5.0,
    micro_crossfade_ms: float = 80.0,
) -> dict[str, Any]:
    """Three-tier cascade for highlight END alignment (symmetric to start)."""
    t_audio_end = None
    anchor_source = "none"
    info = ""
    is_anchor = isinstance(anchor_result, AudioAnchorResult)

    # Tier 1: ASR utterance end
    if is_anchor and anchor_result.status == "ready" and anchor_result.utterances:
        containing = anchor_result.find_containing_utterance(vlm_timestamp)
        if containing:
            t_audio_end = containing.end
            anchor_source = "asr_utt_containing"
            info = f"VLM in utt[{containing.start:.2f}-{containing.end:.2f}]"
        if t_audio_end is None:
            u, d = anchor_result.find_nearest_utterance(vlm_timestamp, search_radius, "before")
            if u and d <= search_radius:
                # Guard: only use this utterance end if VLM is not in a long post-speech gap.
                # If VLM end is > post_speech_grace after utterance end, the VLM intentionally
                # included silence/action; snapping back would clip that content.
                post_speech_gap = vlm_timestamp - u.end
                if post_speech_gap <= 1.2:
                    t_audio_end = u.end
                    anchor_source = "asr_utt_nearest"
                    info = f"Nearest utt ends at {u.end:.2f}s (d={d:.2f}s)"
                else:
                    info = f"Utt ends at {u.end:.2f}s but VLM is {post_speech_gap:.2f}s after (post-speech grace exceeded), using visual"

    # Tier 2: VAD end
    if t_audio_end is None and is_anchor and anchor_result.status == "ready":
        v = anchor_result.find_vad_offset_near(vlm_timestamp, search_radius + 1.0)
        if v is not None and abs(v - vlm_timestamp) <= max_shift:
            # Same guard: reject VAD ends that are too far before VLM (post-speech action)
            post_vad_gap = vlm_timestamp - v
            if post_vad_gap <= 1.5:
                t_audio_end = v
                anchor_source = "vad_fallback"
                info = f"VAD end at {v:.2f}s"
            else:
                info = f"VAD ends at {v:.2f}s but VLM is {post_vad_gap:.2f}s after (post-speech action), using visual"

    if t_audio_end is None and anchor_result and not is_anchor:
        containing = anchor_result.find_containing_interval(vlm_timestamp)
        if containing:
            t_audio_end = containing.end
            anchor_source = "vad_interval"

    # Tier 3
    needs_fade = False
    fade_ms = 0.0

    if t_audio_end is not None and abs(t_audio_end - vlm_timestamp) <= max_shift:
        # ── Visual alignment priority (end symmetric to start) ──
        # Priority 1: Cuts AFTER audio end (tail cuts — camera cuts after word ends → ideal)
        tail_cuts = [c for c in visual_cuts if t_audio_end <= c <= t_audio_end + visual_tail_window]
        # Priority 2: Cuts VERY shortly BEFORE audio end (tight lead, < lead_out_audio+0.05)
        tight_lead_cuts = [c for c in visual_cuts if t_audio_end - lead_out_audio - 0.05 <= c < t_audio_end]
        # Priority 3: Wider lead cuts (up to 0.3s before end — clips the tail of the last word)
        wide_lead_cuts = [c for c in visual_cuts if t_audio_end - 0.30 <= c < t_audio_end - lead_out_audio - 0.05]

        chosen_cut = None
        chosen_was_lead = False

        if tail_cuts:
            chosen_cut = min(tail_cuts)  # earliest after audio end
            chosen_was_lead = False
        elif tight_lead_cuts:
            chosen_cut = max(tight_lead_cuts)  # closest before audio end
            chosen_was_lead = True
        elif wide_lead_cuts:
            best_wide = max(wide_lead_cuts)
            if t_audio_end - best_wide <= 0.30:
                chosen_cut = best_wide
                chosen_was_lead = True

        if chosen_cut is not None:
            final_point = chosen_cut - lead_out_visual
            decision = "visual_cut_aligned"
            gap = abs(t_audio_end - chosen_cut)
            if chosen_was_lead:
                # Cutting before audio ends → clips tail of speech, need fade
                needs_fade = True
                fade_ms = micro_crossfade_ms
            else:
                # Cutting after audio ends → no clipping
                # Fade only if cut is extremely close to audio end (<0.05s)
                needs_fade = gap < 0.05
                fade_ms = micro_crossfade_ms if needs_fade else 0.0
        else:
            final_point = t_audio_end + lead_out_audio
            decision = "audio_end_tail"
            needs_fade = True
            fade_ms = micro_crossfade_ms
    else:
        if visual_cuts:
            closest = min(visual_cuts, key=lambda c: abs(c - vlm_timestamp))
            final_point = closest - lead_out_visual
        else:
            final_point = vlm_timestamp
        decision = "pure_visual_cut"
        anchor_source = "visual_only"
        if t_audio_end is not None and abs(t_audio_end - vlm_timestamp) > max_shift:
            info = f"Audio end at {t_audio_end:.2f}s >{max_shift}s from VLM"
            t_audio_end = None
        needs_fade = False

    if abs(final_point - vlm_timestamp) > max_shift:
        final_point = vlm_timestamp
        decision = "vlm_fallback"
        needs_fade = True
        fade_ms = micro_crossfade_ms

    return {
        "final_end": round(final_point, 3),
        "decision": decision,
        "anchor_source": anchor_source,
        "audio_end": round(t_audio_end, 3) if t_audio_end is not None else None,
        "needs_fade": needs_fade,
        "fade_ms": round(fade_ms, 1) if needs_fade else 0.0,
        "info": info,
    }


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _result_from_dict(data: dict) -> AudioAnchorResult:
    words = [WordTimestamp(**w) for w in data.get("words", [])]
    utterances = [UtteranceBoundary(**u) for u in data.get("utterances", [])]
    vad_segs = [tuple(s) for s in data.get("vad_segments", [])]
    speech_intervals = [
        SpeechInterval(start=iv["start"], end=iv["end"], track="vad_union", confidence=0.9)
        for iv in data.get("speech_intervals", [])
    ]
    return AudioAnchorResult(
        source_path=data.get("source_path", ""),
        source_sha256=data.get("source_sha256", ""),
        status=data.get("status", "error"),
        words=words,
        utterances=utterances,
        vad_segments=vad_segs,
        speech_intervals=speech_intervals,
        bgm_detected=data.get("bgm_detected", False),
        emotion_tokens=data.get("emotion_tokens", []),
        inference_time_s=data.get("inference_time_s", 0),
        asr_time_s=data.get("asr_time_s", 0),
        vad_time_s=data.get("vad_time_s", 0),
        error=data.get("error"),
        config=data.get("config", {}),
        demucs_used=False,
    )


def _result_to_dict(r: AudioAnchorResult) -> dict:
    return {
        "source_path": r.source_path,
        "source_sha256": r.source_sha256,
        "status": r.status,
        "words": [asdict(w) for w in r.words],
        "utterances": [asdict(u) for u in r.utterances],
        "vad_segments": [list(s) for s in r.vad_segments],
        "speech_intervals": [{"start": iv.start, "end": iv.end} for iv in r.speech_intervals],
        "bgm_detected": r.bgm_detected,
        "emotion_tokens": r.emotion_tokens,
        "inference_time_s": r.inference_time_s,
        "asr_time_s": r.asr_time_s,
        "vad_time_s": r.vad_time_s,
        "error": r.error,
        "config": r.config,
        "demucs_used": False,
    }


def _atomic_write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Public serialization helpers
# ---------------------------------------------------------------------------

def result_from_dict(data: dict) -> AudioAnchorResult:
    """Deserialize an AudioAnchorResult from a dict (e.g., loaded from JSON)."""
    return _result_from_dict(data)


def result_to_dict(r: AudioAnchorResult) -> dict:
    """Serialize an AudioAnchorResult to a JSON-serializable dict."""
    return _result_to_dict(r)
