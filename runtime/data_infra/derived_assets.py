"""
Pure, deterministic builders for derived narrative assets.

Each builder is a zero-LLM, zero-side-effect class whose ``build`` classmethod
takes structured input and returns structured output.  All computation is
deterministic — same input always produces the same output.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Timestamp:
    """A point in time within a narrative, measured in seconds."""

    seconds: float

    def __post_init__(self) -> None:
        if self.seconds < 0:
            raise ValueError(f"Timestamp must be non-negative, got {self.seconds}")


@dataclass(frozen=True)
class TimeRange:
    """A closed-open interval [start, end)."""

    start: Timestamp
    end: Timestamp

    def __post_init__(self) -> None:
        if self.start.seconds > self.end.seconds:
            raise ValueError(
                f"start ({self.start.seconds}) must be <= end ({self.end.seconds})"
            )

    @property
    def duration(self) -> float:
        return self.end.seconds - self.start.seconds


@dataclass(frozen=True)
class SegmentRef:
    """Lightweight reference to a narrative segment."""

    segment_id: str
    time_range: TimeRange


# ---------------------------------------------------------------------------
# 1. CharacterAppearanceIndex
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppearanceRecord:
    """A single appearance of a character in one segment."""

    segment_id: str
    time_range: TimeRange
    confidence: float = 1.0  # 0..1, how certain the detection is


@dataclass(frozen=True)
class CharacterAppearanceStats:
    """Aggregated statistics for one character."""

    character_id: str
    first_appearance: Optional[Timestamp]
    last_appearance: Optional[Timestamp]
    total_segments: int
    total_duration: float  # seconds
    segments: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CharacterAppearanceIndex:
    """Index mapping every character to their appearance records and stats.

    Built deterministically from a list of per-segment character detections.
    """

    characters: Dict[str, List[AppearanceRecord]]
    stats: Dict[str, CharacterAppearanceStats]

    @classmethod
    def build(
        cls,
        segments: Iterable[SegmentRef],
        detections: Dict[str, Dict[str, float]],
    ) -> "CharacterAppearanceIndex":
        """Build the index from segment references and per-segment detections.

        Parameters
        ----------
        segments:
            Ordered iterable of segment references.  Order is preserved as the
            narrative timeline.
        detections:
            ``detections[segment_id]`` is a dict mapping ``character_id`` to a
            confidence score in [0, 1].  Characters not mentioned in a segment
            are treated as absent (confidence 0).

        Returns
        -------
        CharacterAppearanceIndex
            The fully built index, deterministic for the same inputs.
        """
        characters: Dict[str, List[AppearanceRecord]] = defaultdict(list)
        segment_map: Dict[str, SegmentRef] = {}

        for seg in segments:
            segment_map[seg.segment_id] = seg
            seg_detections = detections.get(seg.segment_id, {})
            for char_id, confidence in seg_detections.items():
                if confidence <= 0.0:
                    continue
                clamped = min(max(confidence, 0.0), 1.0)
                characters[char_id].append(
                    AppearanceRecord(
                        segment_id=seg.segment_id,
                        time_range=seg.time_range,
                        confidence=clamped,
                    )
                )

        stats: Dict[str, CharacterAppearanceStats] = {}
        for char_id, records in characters.items():
            sorted_records = sorted(records, key=lambda r: r.time_range.start.seconds)
            first = sorted_records[0].time_range.start if sorted_records else None
            last = sorted_records[-1].time_range.end if sorted_records else None
            total_duration = sum(r.time_range.duration for r in sorted_records)
            stats[char_id] = CharacterAppearanceStats(
                character_id=char_id,
                first_appearance=first,
                last_appearance=last,
                total_segments=len(sorted_records),
                total_duration=total_duration,
                segments=[r.segment_id for r in sorted_records],
            )

        return cls(characters=dict(characters), stats=stats)


# ---------------------------------------------------------------------------
# 2. EmotionIntensityCurve
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EmotionPoint:
    """A single emotion data point at a timestamp."""

    segment_id: str
    timestamp: Timestamp
    scores: Dict[str, float]  # emotion_name -> intensity (0..1)


@dataclass(frozen=True)
class CurvePoint:
    """A single point on the final intensity curve."""

    timestamp: Timestamp
    intensity: float  # 0..1  overall emotional intensity
    dominant_emotion: str
    valence: float  # -1..1  negative → positive
    arousal: float  # 0..1   calm → excited


@dataclass(frozen=True)
class Peak:
    """A detected intensity peak."""

    timestamp: Timestamp
    intensity: float
    width: float  # seconds — width of the peak at half-max
    prominence: float  # how much it stands out from neighbours


@dataclass(frozen=True)
class CurveStatistics:
    """Summary statistics for an emotion intensity curve."""

    mean_intensity: float
    max_intensity: float
    min_intensity: float
    variance: float
    mean_valence: float
    mean_arousal: float


@dataclass(frozen=True)
class EmotionIntensityCurve:
    """A deterministic emotion intensity curve derived from per-segment scores.

    The curve is point-for-point deterministic: no smoothing, no windowing,
    no random sampling.  Every computed value is a closed-form function of
    the input.
    """

    curve: List[CurvePoint]
    peaks: List[Peak]
    statistics: CurveStatistics

    # Recognised emotion families for valence / arousal heuristics.
    _EMOTION_VALENCE_AROUSAL: Dict[str, Tuple[float, float]] = {
        "joy": (0.8, 0.7),
        "happiness": (0.8, 0.7),
        "excitement": (0.7, 0.9),
        "surprise": (0.3, 0.8),
        "anticipation": (0.4, 0.6),
        "trust": (0.6, 0.3),
        "love": (0.9, 0.6),
        "admiration": (0.7, 0.4),
        "gratitude": (0.8, 0.4),
        "relief": (0.7, 0.2),
        "hope": (0.5, 0.5),
        "pride": (0.6, 0.6),
        "anger": (-0.7, 0.8),
        "rage": (-0.9, 0.9),
        "fear": (-0.7, 0.8),
        "terror": (-0.9, 0.9),
        "sadness": (-0.7, 0.1),
        "grief": (-0.9, 0.1),
        "disgust": (-0.6, 0.5),
        "contempt": (-0.7, 0.4),
        "shame": (-0.5, 0.3),
        "guilt": (-0.6, 0.3),
        "anxiety": (-0.6, 0.7),
        "confusion": (-0.1, 0.5),
        "neutral": (0.0, 0.0),
        "calm": (0.3, 0.1),
        "boredom": (-0.2, 0.0),
        "interest": (0.4, 0.5),
        "curiosity": (0.4, 0.6),
    }

    _DEFAULT_VA: Tuple[float, float] = (0.0, 0.5)

    @classmethod
    def _dominant_emotion(cls, scores: Dict[str, float]) -> str:
        if not scores:
            return "neutral"
        return max(scores, key=lambda k: scores[k])

    @classmethod
    def _overall_intensity(cls, scores: Dict[str, float]) -> float:
        """Mean of top-3 emotion scores, clipped to [0, 1]."""
        if not scores:
            return 0.0
        top = sorted(scores.values(), reverse=True)[:3]
        return min(max(sum(top) / len(top), 0.0), 1.0)

    @classmethod
    def _valence_arousal_from_scores(
        cls, scores: Dict[str, float]
    ) -> Tuple[float, float]:
        """Weighted average of known emotion valence/arousal values."""
        if not scores:
            return cls._DEFAULT_VA
        total = sum(scores.values())
        if total == 0.0:
            return cls._DEFAULT_VA
        v_total = 0.0
        a_total = 0.0
        for name, intensity in scores.items():
            v, a = cls._EMOTION_VALENCE_AROUSAL.get(name.lower(), cls._DEFAULT_VA)
            w = intensity / total
            v_total += v * w
            a_total += a * w
        return (v_total, a_total)

    @classmethod
    def _detect_peaks(
        cls,
        points: Sequence[CurvePoint],
        *,
        min_prominence: float = 0.15,
        min_intensity: float = 0.3,
    ) -> List[Peak]:
        """Simple prominence-based peak detection — fully deterministic."""
        if len(points) < 3:
            return []

        intensities = [p.intensity for p in points]
        peaks: List[Peak] = []

        for i in range(1, len(intensities) - 1):
            if intensities[i] <= intensities[i - 1] or intensities[i] <= intensities[i + 1]:
                continue
            if intensities[i] < min_intensity:
                continue

            # Prominence: how much this point rises above the higher of the
            # two surrounding valleys.
            left_min = min(intensities[:i]) if i > 0 else intensities[i]
            right_min = min(intensities[i + 1:]) if i + 1 < len(intensities) else intensities[i]
            valley = max(left_min, right_min)
            prominence = intensities[i] - valley
            if prominence < min_prominence:
                continue

            # Width at half-max
            half = intensities[i] / 2.0
            left = i
            while left > 0 and intensities[left - 1] > half:
                left -= 1
            right = i
            while right < len(intensities) - 1 and intensities[right + 1] > half:
                right += 1
            width = (points[right].timestamp.seconds - points[left].timestamp.seconds)

            peaks.append(
                Peak(
                    timestamp=points[i].timestamp,
                    intensity=intensities[i],
                    width=width,
                    prominence=prominence,
                )
            )

        return peaks

    @classmethod
    def build(
        cls,
        emotion_points: Iterable[EmotionPoint],
        *,
        min_prominence: float = 0.15,
        min_intensity: float = 0.3,
    ) -> "EmotionIntensityCurve":
        """Build the intensity curve from ordered emotion data points.

        Parameters
        ----------
        emotion_points:
            Ordered iterable of emotion data points.  Timestamp order must
            be non-decreasing.
        min_prominence:
            Minimum prominence for a peak to be detected (0..1).
        min_intensity:
            Minimum absolute intensity for a peak to be considered.

        Returns
        -------
        EmotionIntensityCurve
            Deterministic curve, peaks, and statistics.
        """
        curve: List[CurvePoint] = []
        for ep in emotion_points:
            dominant = cls._dominant_emotion(ep.scores)
            intensity = cls._overall_intensity(ep.scores)
            valence, arousal = cls._valence_arousal_from_scores(ep.scores)
            curve.append(
                CurvePoint(
                    timestamp=ep.timestamp,
                    intensity=intensity,
                    dominant_emotion=dominant,
                    valence=valence,
                    arousal=arousal,
                )
            )

        peaks = cls._detect_peaks(
            curve, min_prominence=min_prominence, min_intensity=min_intensity
        )

        n = len(curve)
        if n == 0:
            statistics = CurveStatistics(
                mean_intensity=0.0,
                max_intensity=0.0,
                min_intensity=0.0,
                variance=0.0,
                mean_valence=0.0,
                mean_arousal=0.0,
            )
        else:
            intensities = [p.intensity for p in curve]
            valences = [p.valence for p in curve]
            arousals = [p.arousal for p in curve]

            mean_i = sum(intensities) / n
            mean_v = sum(valences) / n
            mean_a = sum(arousals) / n
            var_i = sum((x - mean_i) ** 2 for x in intensities) / n

            statistics = CurveStatistics(
                mean_intensity=mean_i,
                max_intensity=max(intensities),
                min_intensity=min(intensities),
                variance=var_i,
                mean_valence=mean_v,
                mean_arousal=mean_a,
            )

        return cls(curve=curve, peaks=peaks, statistics=statistics)


# ---------------------------------------------------------------------------
# 3. RelationshipTimeline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RelationshipEvent:
    """A single interaction event between two characters."""

    segment_id: str
    timestamp: Timestamp
    character_a: str
    character_b: str
    interaction_type: str  # e.g. "dialogue", "conflict", "cooperation", "proximity"
    intensity: float  # 0..1, strength of the interaction
    sentiment: float  # -1..1, negative → positive


@dataclass(frozen=True)
class RelationshipState:
    """The computed relationship state over a time span."""

    character_a: str
    character_b: str
    start_time: Timestamp
    end_time: Timestamp
    state: str  # e.g. "allies", "enemies", "neutral", "estranged", "intimate"
    confidence: float  # 0..1
    sentiment: float  # -1..1


@dataclass(frozen=True)
class PairSummary:
    """Aggregate summary for a character pair."""

    character_a: str
    character_b: str
    total_interactions: int
    mean_sentiment: float
    mean_intensity: float
    state_durations: Dict[str, float]  # state -> total seconds
    dominant_state: str


@dataclass(frozen=True)
class RelationshipTimeline:
    """Deterministic timeline of relationships between every character pair.

    Built from a chronologically ordered sequence of interaction events.
    """

    states: Dict[Tuple[str, str], List[RelationshipState]]
    summaries: Dict[Tuple[str, str], PairSummary]
    all_events: List[RelationshipEvent]

    # ------------------------------------------------------------------
    # State classification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_pair(a: str, b: str) -> Tuple[str, str]:
        """Return a canonical (sorted) pair tuple so (A,B) == (B,A)."""
        return (a, b) if a <= b else (b, a)

    @classmethod
    def _classify_state(cls, sentiment: float, intensity: float) -> str:
        """Classify relationship state from sentiment and intensity.

        Rules are deterministic threshold-based lookups.
        """
        if intensity < 0.15:
            return "neutral"
        if sentiment >= 0.6:
            return "intimate" if intensity >= 0.7 else "allies"
        if sentiment >= 0.2:
            return "allies"
        if sentiment >= -0.2:
            return "neutral"
        if sentiment >= -0.6:
            return "estranged" if intensity >= 0.5 else "neutral"
        return "enemies"

    @classmethod
    def _running_window(
        cls,
        events: Sequence[RelationshipEvent],
        window_size: int = 3,
    ) -> List[RelationshipState]:
        """Apply a sliding window to produce stable state segments."""
        if not events:
            return []

        # Sort events by timestamp
        sorted_events = sorted(events, key=lambda e: e.timestamp.seconds)

        # Compute rolling sentiment/intensity
        states: List[RelationshipState] = []
        half = window_size // 2

        for i, ev in enumerate(sorted_events):
            lo = max(0, i - half)
            hi = min(len(sorted_events), i + half + 1)
            window = sorted_events[lo:hi]

            avg_sentiment = sum(e.sentiment for e in window) / len(window)
            avg_intensity = sum(e.intensity for e in window) / len(window)
            state_name = cls._classify_state(avg_sentiment, avg_intensity)

            # Compute confidence: how consistent is the window?
            sentiment_variance = (
                sum((e.sentiment - avg_sentiment) ** 2 for e in window) / len(window)
            )
            confidence = max(0.0, 1.0 - math.sqrt(sentiment_variance))

            states.append(
                RelationshipState(
                    character_a=ev.character_a,
                    character_b=ev.character_b,
                    start_time=ev.timestamp,
                    end_time=ev.timestamp,
                    state=state_name,
                    confidence=confidence,
                    sentiment=avg_sentiment,
                )
            )

        return states

    @classmethod
    def _merge_adjacent_states(
        cls, states: List[RelationshipState]
    ) -> List[RelationshipState]:
        """Merge consecutive states with the same label."""
        if not states:
            return []

        merged: List[RelationshipState] = [states[0]]
        for s in states[1:]:
            prev = merged[-1]
            if s.state == prev.state:
                merged[-1] = RelationshipState(
                    character_a=prev.character_a,
                    character_b=prev.character_b,
                    start_time=prev.start_time,
                    end_time=s.end_time,
                    state=prev.state,
                    confidence=(prev.confidence + s.confidence) / 2.0,
                    sentiment=(prev.sentiment + s.sentiment) / 2.0,
                )
            else:
                merged.append(s)
        return merged

    @classmethod
    def build(
        cls,
        events: Iterable[RelationshipEvent],
        *,
        window_size: int = 3,
    ) -> "RelationshipTimeline":
        """Build the relationship timeline from interaction events.

        Parameters
        ----------
        events:
            Chronologically ordered interaction events.  Each event links two
            characters and carries a type, intensity, and sentiment.
        window_size:
            Sliding window size (in events) for computing stable state labels.
            Must be >= 1 (will be clamped to 1 if needed).

        Returns
        -------
        RelationshipTimeline
            Deterministic timeline of relationship states and summaries.
        """
        ws = max(1, window_size)
        all_events = sorted(events, key=lambda e: e.timestamp.seconds)

        # Group events by canonical pair
        pair_events: Dict[Tuple[str, str], List[RelationshipEvent]] = defaultdict(list)
        for ev in all_events:
            key = cls._canonical_pair(ev.character_a, ev.character_b)
            pair_events[key].append(ev)

        all_states: Dict[Tuple[str, str], List[RelationshipState]] = {}
        all_summaries: Dict[Tuple[str, str], PairSummary] = {}

        for pair_key, pair_evs in pair_events.items():
            raw_states = cls._running_window(pair_evs, window_size=ws)
            merged_states = cls._merge_adjacent_states(raw_states)
            all_states[pair_key] = merged_states

            # Build summary
            total = len(pair_evs)
            mean_sent = sum(e.sentiment for e in pair_evs) / total if total else 0.0
            mean_int = sum(e.intensity for e in pair_evs) / total if total else 0.0

            state_durations: Dict[str, float] = defaultdict(float)
            for s in merged_states:
                dur = s.end_time.seconds - s.start_time.seconds
                state_durations[s.state] += dur

            dominant_state = (
                max(state_durations, key=lambda k: state_durations[k])
                if state_durations
                else "neutral"
            )

            all_summaries[pair_key] = PairSummary(
                character_a=pair_key[0],
                character_b=pair_key[1],
                total_interactions=total,
                mean_sentiment=mean_sent,
                mean_intensity=mean_int,
                state_durations=dict(state_durations),
                dominant_state=dominant_state,
            )

        return cls(states=all_states, summaries=all_summaries, all_events=all_events)


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def build_derived_assets(
    segments: Iterable[SegmentRef],
    detections: Dict[str, Dict[str, float]],
    emotion_points: Iterable[EmotionPoint],
    relationship_events: Iterable[RelationshipEvent],
    *,
    peak_min_prominence: float = 0.15,
    peak_min_intensity: float = 0.3,
    relationship_window: int = 3,
) -> Tuple[CharacterAppearanceIndex, EmotionIntensityCurve, RelationshipTimeline]:
    """Build all three derived assets in one call.

    Returns a tuple of (CharacterAppearanceIndex, EmotionIntensityCurve,
    RelationshipTimeline).  All three are deterministic pure functions of
    the inputs.
    """
    return (
        CharacterAppearanceIndex.build(segments, detections),
        EmotionIntensityCurve.build(
            emotion_points,
            min_prominence=peak_min_prominence,
            min_intensity=peak_min_intensity,
        ),
        RelationshipTimeline.build(
            relationship_events, window_size=relationship_window
        ),
    )