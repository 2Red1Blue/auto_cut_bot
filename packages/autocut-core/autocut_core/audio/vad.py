"""Voice Activity Detection with pluggable detectors.

Current implementation: Demucs source separation + Silero VAD (dual-track).
Future: SAMAudioDetector can be added as a drop-in replacement.

Architecture:
- Detectors run in a subprocess using a dedicated VAD virtualenv (.venv-audio-boundary)
  to avoid torch/demucs dependency conflicts with the main pipeline.
- Results are cached by source video SHA256 + detector config hash.
- The main pipeline process only sees lightweight dict/list results, never imports torch.
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
from typing import Any, Protocol

from autocut_core.contracts.audio_boundary import AudioBoundaryPolicy


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SpeechInterval:
    """A detected speech segment."""
    start: float
    end: float
    track: str = "union"  # "original_mix" | "demucs_vocals" | "no_vocals" | "union"
    confidence: float = 1.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def contains(self, t: float, pad: float = 0.0) -> bool:
        return (self.start - pad) <= t <= (self.end + pad)


@dataclass
class VADResult:
    """Result of VAD analysis for a single source."""
    source_path: str
    source_sha256: str
    status: str  # "ready" | "no_audio" | "error"
    speech_intervals: list[SpeechInterval] = field(default_factory=list)
    track_intervals: dict[str, list[SpeechInterval]] = field(default_factory=dict)
    demucs_used: bool = False
    error: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def is_in_speech(self, t: float, pad: float = 0.0) -> bool:
        """Check if time t falls within any speech interval."""
        return any(iv.contains(t, pad=pad) for iv in self.speech_intervals)

    def find_containing_interval(self, t: float) -> SpeechInterval | None:
        """Return the speech interval containing time t, or None."""
        for iv in sorted(self.speech_intervals, key=lambda x: x.start):
            if iv.contains(t):
                return iv
        return None

    def find_speech_start_before(self, t: float, max_search: float = 15.0) -> float | None:
        """Find the start of the speech segment that contains or precedes t.

        Walks backward from t through speech intervals to find where speech began.
        Returns None if no speech found within max_search seconds.
        """
        best_start = None
        for iv in self.speech_intervals:
            if iv.start > t:
                break
            if iv.end >= t - 0.01:  # interval reaches or contains t
                best_start = iv.start
            elif iv.end > t - max_search and best_start is None:
                continue
        return best_start

    def find_silence_before(self, t: float, max_search: float = 15.0,
                            min_silence: float = 0.3) -> float | None:
        """Find the last silence gap before time t (suitable for a safe cut point).

        Returns the midpoint of the nearest silence gap, or None.
        """
        # Build a timeline: iterate speech intervals to find gaps
        prev_end = 0.0
        for iv in sorted(self.speech_intervals, key=lambda x: x.start):
            gap_start = prev_end
            gap_end = iv.start
            gap_dur = gap_end - gap_start
            if gap_dur >= min_silence and gap_end <= t and gap_end > t - max_search:
                # This gap is before t and within search range
                return gap_start + gap_dur / 2  # midpoint of gap
            prev_end = max(prev_end, iv.end)
        # Check silence at beginning
        if t > 0 and self.speech_intervals and self.speech_intervals[0].start > 0:
            return self.speech_intervals[0].start / 2
        return None


# ---------------------------------------------------------------------------
# Detector interface (pluggable)
# ---------------------------------------------------------------------------

class SpeechDetector(Protocol):
    """Abstract speech detector. Implementations can be Demucs+Silero, SAM Audio, etc."""

    def detect(self, source_path: Path, *, force: bool = False) -> VADResult:
        """Run VAD on a source video, returning speech intervals."""
        ...

    def detect_batch(self, source_paths: list[Path], *, force: bool = False) -> dict[str, VADResult]:
        """Run VAD on multiple sources. Keyed by source path string."""
        ...


# ---------------------------------------------------------------------------
# Demucs + Silero VAD implementation
# ---------------------------------------------------------------------------

class DemucsSileroDetector:
    """Speech detection via Demucs source separation + Silero VAD.

    Runs the detection in a subprocess using a dedicated venv (.venv-audio-boundary)
    to isolate torch/demucs dependencies from the main pipeline.

    Tracks detected:
    - demucs_vocals: primary source (separated vocals, highest precision for speech)
    - original_mix: fallback (captures speech Demucs might misclassify)
    - no_vocals: optional (detects shouted speech that leaks to accompaniment)
    """

    ENGINE_VERSION = "5.0"  # bump when algorithm changes to invalidate cache

    # Smart merge defaults (tuned for short-drama VAD fusion)
    # See docs/vad-parameters.md for rationale
    DEFAULT_EXTEND_WINDOW: float = 1.5
    DEFAULT_PHRASE_GAP: float = 0.15
    DEFAULT_MERGE_MIN_GAP: float = 0.15

    def __init__(
        self,
        *,
        vad_python: Path | None = None,
        cache_dir: Path | None = None,
        device: str = "cpu",
        policy: AudioBoundaryPolicy | None = None,
        worker_script: Path | None = None,
        model_cache_dir: Path | None = None,
        extend_window: float | None = None,
        phrase_gap: float | None = None,
        merge_min_gap: float | None = None,
    ):
        self.policy = policy or AudioBoundaryPolicy()
        self.device = device
        # Smart merge parameters (controlling how demucs/original intervals merge)
        self.extend_window = float(extend_window if extend_window is not None else self.DEFAULT_EXTEND_WINDOW)
        self.phrase_gap = float(phrase_gap if phrase_gap is not None else self.DEFAULT_PHRASE_GAP)
        self.merge_min_gap = float(merge_min_gap if merge_min_gap is not None else self.DEFAULT_MERGE_MIN_GAP)

        # Resolve VAD python
        if vad_python is not None:
            self.vad_python = Path(vad_python)
        else:
            # Default: .venv-audio-boundary/bin/python in project root
            self.vad_python = Path(".venv-audio-boundary/bin/python").resolve()

        # Worker script path
        if worker_script is not None:
            self.worker_script = Path(worker_script)
        else:
            self.worker_script = Path(__file__).parent / "vad_worker.py"

        # Cache directory for VAD results
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = Path(".sd-cache/vad").resolve()

        # Model cache (torch hub, silero model, demucs weights)
        self.model_cache_dir = model_cache_dir or self.cache_dir / "_models"

    def _sha256_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _config_hash(self) -> str:
        """Hash of VAD configuration for cache invalidation."""
        cfg = {
            "engine": self.ENGINE_VERSION,
            "demucs_model": self.policy.demucs_model,
            "vad_threshold": self.policy.vad_threshold,
            "vad_threshold_original": self.policy.vad_threshold_original,
            "vad_threshold_no_vocals": self.policy.vad_threshold_no_vocals,
            "min_speech_ms": self.policy.min_speech_duration_ms,
            "min_silence_ms": self.policy.min_silence_duration_ms,
            "speech_pad_ms": self.policy.speech_pad_ms,
            "min_safe_gap": self.policy.minimum_safe_gap_seconds,
            "extend_window": self.extend_window,
            "phrase_gap": self.phrase_gap,
            "merge_min_gap": self.merge_min_gap,
            "device": self.device,
        }
        return hashlib.sha256(
            json.dumps(cfg, sort_keys=True).encode()
        ).hexdigest()[:16]

    def _cache_path(self, source_sha: str) -> Path:
        cfg_hash = self._config_hash()
        return self.cache_dir / cfg_hash / source_sha[:2] / f"{source_sha}.json"

    def detect(self, source_path: Path, *, force: bool = False) -> VADResult:
        source_path = Path(source_path).resolve()
        if not source_path.exists():
            return VADResult(
                source_path=str(source_path),
                source_sha256="",
                status="error",
                error=f"File not found: {source_path}",
            )

        source_sha = self._sha256_file(source_path)
        cache_file = self._cache_path(source_sha)

        # Check cache
        if not force and cache_file.is_file():
            try:
                data = json.loads(cache_file.read_text())
                return self._from_cache(data, source_path, source_sha)
            except (json.JSONDecodeError, KeyError):
                pass  # corrupt cache, rebuild

        # Run VAD worker in subprocess
        return self._run_worker(source_path, source_sha, cache_file)

    def detect_batch(self, source_paths: list[Path], *, force: bool = False) -> dict[str, VADResult]:
        results = {}
        for p in source_paths:
            results[str(p)] = self.detect(p, force=force)
        return results

    def _run_worker(self, source_path: Path, source_sha: str, cache_file: Path) -> VADResult:
        """Execute vad_worker.py in the VAD venv and return parsed results."""
        work_dir = self.cache_dir / "_work" / source_sha[:2] / source_sha
        work_dir.mkdir(parents=True, exist_ok=True)
        out_file = work_dir / "vad_result.json"
        model_cache = self.model_cache_dir
        model_cache.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(self.vad_python),
            str(self.worker_script),
            "--source", str(source_path),
            "--work-dir", str(work_dir),
            "--cache-dir", str(model_cache),
            "--device", self.device,
            "--out", str(out_file),
            "--demucs-model", self.policy.demucs_model,
            "--vad-threshold-demucs", str(self.policy.vad_threshold),
            "--vad-threshold-original", str(self.policy.vad_threshold_original),
            "--vad-threshold-no-vocals", str(self.policy.vad_threshold_no_vocals),
            "--min-speech-ms", str(self.policy.min_speech_duration_ms),
            "--min-silence-ms", str(self.policy.min_silence_duration_ms),
            "--speech-pad-ms", str(self.policy.speech_pad_ms),
            "--min-safe-gap", str(self.policy.minimum_safe_gap_seconds),
        ]

        try:
            # Set model cache env vars. TORCH_HOME and HF_HOME point to our
            # persistent cache dir for writes. HOME is NOT overridden so that
            # pre-downloaded models in ~/.cache (from manual demucs runs) are
            # still readable.
            worker_env = {
                **os.environ,
                "TORCH_HOME": str(model_cache / "torch"),
                "HF_HOME": str(model_cache / "huggingface"),
                # Also point HF_HUB_CACHE to existing user cache as fallback
                "HF_HUB_CACHE": os.path.expanduser("~/.cache/huggingface/hub"),
                "TRANSFORMERS_CACHE": os.path.expanduser("~/.cache/huggingface/hub"),
            }
            result = subprocess.run(
                cmd, check=True, capture_output=True, text=True, timeout=600,
                env=worker_env,
            )
        except subprocess.CalledProcessError as e:
            return VADResult(
                source_path=str(source_path),
                source_sha256=source_sha,
                status="error",
                error=f"VAD worker failed (exit {e.returncode}): {e.stderr[-500:]}",
            )
        except subprocess.TimeoutExpired:
            return VADResult(
                source_path=str(source_path),
                source_sha256=source_sha,
                status="error",
                error="VAD worker timed out (300s)",
            )

        if not out_file.is_file():
            return VADResult(
                source_path=str(source_path),
                source_sha256=source_sha,
                status="error",
                error="VAD worker produced no output",
            )

        data = json.loads(out_file.read_text())
        vad_result = self._from_worker_output(data, source_path, source_sha)

        # Write to cache
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(cache_file, self._to_cache(vad_result))

        return vad_result

    def _from_worker_output(self, data: dict, source_path: Path, source_sha: str) -> VADResult:
        def _ivs(items, track):
            return [SpeechInterval(start=float(i["start"]), end=float(i["end"]), track=track)
                    for i in items]

        track_intervals = {}
        for track_name, items in data.get("track_intervals", {}).items():
            track_intervals[track_name] = _ivs(items, track_name)

        # Build union from demucs_vocals primarily, original_mix as supplement
        demucs = track_intervals.get("demucs_vocals", [])
        original = track_intervals.get("original_mix", [])

        if demucs:
            # Smart merge: demucs_vocals is primary (high precision for gaps).
            # original_mix is used only to EXTEND boundaries (catch speech that
            # Demucs misses at segment starts/ends due to SFX/reverb).
            # We do NOT let original_mix fill gaps that demucs identifies as silence,
            # because original_mix has BGM false positives.
            union = self._smart_merge(
                demucs, original, track_intervals.get("no_vocals", []),
                extend_window=self.extend_window,
                min_gap=self.merge_min_gap,
                phrase_gap=self.phrase_gap,
            )
        else:
            union = _ivs(data.get("speech_intervals", []), "union")

        return VADResult(
            source_path=str(source_path),
            source_sha256=source_sha,
            status=data.get("status", "ready"),
            speech_intervals=union,
            track_intervals=track_intervals,
            demucs_used=data.get("demucs_used", False),
            config=data.get("config", {}),
        )

    def _from_cache(self, data: dict, source_path: Path, source_sha: str) -> VADResult:
        def _ivs(items):
            return [SpeechInterval(
                start=float(i["start"]), end=float(i["end"]),
                track=i.get("track", "union"),
                confidence=i.get("confidence", 1.0),
            ) for i in items]

        track_intervals = {}
        for track_name, items in data.get("track_intervals", {}).items():
            track_intervals[track_name] = _ivs(items)

        return VADResult(
            source_path=str(source_path),
            source_sha256=source_sha,
            status=data.get("status", "ready"),
            speech_intervals=_ivs(data.get("speech_intervals", [])),
            track_intervals=track_intervals,
            demucs_used=data.get("demucs_used", False),
            config=data.get("config", {}),
        )

    def _to_cache(self, result: VADResult) -> dict:
        def _iv_dicts(ivs):
            return [{"start": iv.start, "end": iv.end, "track": iv.track,
                     "confidence": iv.confidence} for iv in ivs]

        return {
            "source_path": result.source_path,
            "source_sha256": result.source_sha256,
            "status": result.status,
            "speech_intervals": _iv_dicts(result.speech_intervals),
            "track_intervals": {k: _iv_dicts(v) for k, v in result.track_intervals.items()},
            "demucs_used": result.demucs_used,
            "config": result.config,
        }


    @staticmethod
    def _smart_merge(
        demucs: list[SpeechInterval],
        original: list[SpeechInterval],
        no_vocals: list[SpeechInterval] | None = None,
        extend_window: float = 1.5,
        min_gap: float = 0.15,
        phrase_gap: float = 0.15,
    ) -> list[SpeechInterval]:
        """Merge speech intervals using demucs_vocals as structural guide.

        Design principle: MERGE CONSERVATIVE, SNAP INTELLIGENT.
        - Merge stage only combines intervals within phrase_gap (word-internal pauses).
          Real speech gaps (>0.15s) are preserved so snap logic can decide which
          gaps to cross (see _find_speech_boundary_start gap_threshold).
        - demucs_vocals is authoritative for gap structure (BGM removed).
        - original_mix extends phrase boundaries by at most extend_window (1.5s)
          to catch word-initial consonants / breath that Demucs may truncate.
        - original_mix NEVER bridges gaps that demucs identifies as silence,
          because original_mix contains BGM/SFX false positives.

        Steps:
        1. Group demucs_vocals intervals into phrases (gap <= phrase_gap = same phrase).
        2. Pad original_mix intervals with speech_pad semantics and group same way.
        3. For each demucs phrase, extend start/end using overlapping/adjoining
           original phrases, but HARD-CAP at neighboring demucs phrase boundaries.
           Extension is limited to extend_window to prevent BGM false positives
           from bridging real gaps.
        4. Merge resulting phrases within min_gap (extension may bring phrases close).

        Parameter rationale (see work_ai/ac_auto_cut/原理/vad-audio-snap-design.md §5 for full tuning):
        - phrase_gap=0.15s: demucs gaps <=0.15s are intra-word pauses (e.g., plosives).
        - extend_window=1.5s: max extension for word-initial/final truncation fix.
          1.5s covers weak onsets (e.g., ep07@79.4s "Go" buried under BGM).
          SAFETY_MARGIN (cap to adjacent demucs phrase) prevents over-bridging.
        - min_gap=0.15s: final merge only combines phrases brought within 0.15s
          by extension (catches near-overlaps from boundary adjustment).

        Args:
            demucs: Speech intervals from demucs_vocals track (primary, gap authority).
            original: Speech intervals from original_mix track (boundary supplement).
            no_vocals: Speech intervals from no_vocals track (shouted speech fallback).
            extend_window: Max seconds to extend phrase boundaries via supplementary.
            min_gap: Minimum gap between final merged phrases.
            phrase_gap: Max gap between demucs intervals within one phrase.

        Returns:
            Merged speech intervals preserving demucs gap structure.
        """
        if not demucs:
            all_ivs = list(original) + list(no_vocals or [])
            all_ivs.sort(key=lambda x: x.start)
            return DemucsSileroDetector._merge_intervals(all_ivs, min_gap)

        supplementary = list(original) + list(no_vocals or [])

        # Step 1: Group demucs intervals into phrases (phrase_gap = same phrase)
        sorted_d = sorted(demucs, key=lambda x: x.start)
        d_phrases = [[sorted_d[0]]]
        for iv in sorted_d[1:]:
            if iv.start - d_phrases[-1][-1].end <= phrase_gap:
                d_phrases[-1].append(iv)
            else:
                d_phrases.append([iv])

        # Compute demucs phrase spans
        d_spans = []  # (phrase_start, phrase_end) for each demucs phrase
        for ph in d_phrases:
            d_spans.append((ph[0].start, max(iv.end for iv in ph)))

        # Step 2: Group supplementary intervals into phrases (same phrase_gap)
        sorted_s = sorted(supplementary, key=lambda x: x.start)
        s_phrases = []
        if sorted_s:
            cur = [sorted_s[0]]
            for iv in sorted_s[1:]:
                if iv.start - cur[-1].end <= phrase_gap:
                    cur.append(iv)
                else:
                    s_phrases.append(cur)
                    cur = [iv]
            s_phrases.append(cur)
        s_spans = []
        for ph in s_phrases:
            s_spans.append((ph[0].start, max(iv.end for iv in ph)))

        # Step 3: Extend each demucs phrase using supplementary phrases
        # CRITICAL: extension is bounded by:
        #   a) neighboring demucs phrase boundaries (prev_cap, next_cap)
        #   b) extend_window (max extension distance per side)
        SAFETY_MARGIN = 0.3  # 0.3s safety gap between extended phrases (prevents bridging real pauses)
        result_phrases = []
        for pi, (d_start, d_end) in enumerate(d_spans):
            p_start = d_start
            p_end = d_end

            # Determine hard caps from neighboring demucs phrases
            prev_cap = d_spans[pi-1][1] + SAFETY_MARGIN if pi > 0 else 0.0
            next_cap = d_spans[pi+1][0] - SAFETY_MARGIN if pi < len(d_spans)-1 else float('inf')

            # Extend with supplementary phrases
            for s_s, s_e in s_spans:
                # Overlap case: supplementary phrase overlaps demucs phrase
                if s_e > p_start and s_s < p_end:
                    if s_s < p_start and s_s >= prev_cap and (p_start - s_s) <= extend_window:
                        p_start = s_s
                    if s_e > p_end and s_e <= next_cap and (s_e - p_end) <= extend_window:
                        p_end = s_e
                # Adjoin start: supplementary ends just before/at phrase start
                elif s_e >= p_start - extend_window and s_s < p_start and s_e > prev_cap:
                    if s_s >= prev_cap and (p_start - s_s) <= extend_window:
                        p_start = s_s
                # Adjoin end: supplementary starts just after/at phrase end
                elif s_s <= p_end + extend_window and s_e > p_end and s_s < next_cap:
                    if s_e <= next_cap and (s_e - p_end) <= extend_window:
                        p_end = s_e

            result_phrases.append(SpeechInterval(start=p_start, end=p_end, track="union"))

        # Step 4: Merge phrases that are within min_gap after extension
        result_phrases.sort(key=lambda x: x.start)
        merged = []
        for ph in result_phrases:
            if merged and ph.start - merged[-1].end <= min_gap:
                merged[-1].end = max(merged[-1].end, ph.end)
            else:
                merged.append(ph)

        return merged


    @staticmethod
    def _merge_intervals(intervals: list[SpeechInterval], min_gap: float) -> list[SpeechInterval]:
        if not intervals:
            return []
        merged = []
        for iv in sorted(intervals, key=lambda x: x.start):
            if not merged or iv.start - merged[-1].end >= min_gap:
                merged.append(SpeechInterval(start=iv.start, end=iv.end, track="union"))
            else:
                merged[-1].end = max(merged[-1].end, iv.end)
        return merged

    @staticmethod
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
# Factory
# ---------------------------------------------------------------------------

def get_vad_detector(
    backend: str = "demucs_silero",
    *,
    vad_python: Path | None = None,
    cache_dir: Path | None = None,
    device: str = "cpu",
    policy: AudioBoundaryPolicy | None = None,
    extend_window: float | None = None,
    phrase_gap: float | None = None,
    merge_min_gap: float | None = None,
) -> SpeechDetector:
    """Factory: create a VAD detector by backend name.

    Args:
        backend: "demucs_silero" (Demucs separation + Silero VAD, legacy) |
                 "asr_anchor" (SenseVoice ASR + fsmn-vad, recommended — 100x faster, higher precision) |
                 "none" (returns None-speech detector)
        vad_python: Path to VAD venv python executable
        cache_dir: VAD result cache directory
        device: torch device (cpu/mps/cuda)
        policy: AudioBoundaryPolicy for VAD parameters
        extend_window: Max supplementary track extension (overrides detector default)
        phrase_gap: Max gap within a demucs phrase (overrides detector default)
        merge_min_gap: Min gap between final merged phrases (overrides detector default)
    """
    if backend == "demucs_silero":
        return DemucsSileroDetector(
            vad_python=vad_python, cache_dir=cache_dir,
            device=device, policy=policy,
            extend_window=extend_window,
            phrase_gap=phrase_gap,
            merge_min_gap=merge_min_gap,
        )
    elif backend == "asr_anchor":
        from autocut_core.audio.asr_anchor import ASRAnchorDetector
        return ASRAnchorDetector(
            vad_python=vad_python, cache_dir=cache_dir,
            device=device, policy=policy,
        )
    elif backend == "none":
        return _NullDetector()
    else:
        raise ValueError(f"Unknown VAD backend: {backend}")


class _NullDetector:
    """No-op detector that returns empty results (for when VAD is disabled)."""

    def detect(self, source_path: Path, *, force: bool = False) -> VADResult:
        return VADResult(
            source_path=str(source_path), source_sha256="", status="disabled",
        )

    def detect_batch(self, source_paths, *, force=False):
        return {str(p): self.detect(p) for p in source_paths}
