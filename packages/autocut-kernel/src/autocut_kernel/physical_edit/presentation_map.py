"""Physical consumption of the existing v2 piecewise presentation certificate.

Replay proves consistency of supplied values, not Store commitment or physical
admission. Production Commands must obtain these values from the exact reader.
No root mutation, duration scaling, or synthetic v1 certificate is involved.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction

from ..media.root_evidence import RootMediaEvidenceBundle
from ..media.stage4_predecessor import (
    CommittedVideoToAudioClockMapCertificate,
    PresentationTimelineProbe,
)
from ..media.timed_evidence import CalibrationBinding
from ..media.types import MediaValidationError, TickRange, TimeBase, require_pts


class PresentationMapValidationError(MediaValidationError):
    """An endpoint or complete span lacks proven source-presentation coverage."""


def _presentation(tick: int, time_base: TimeBase) -> Fraction:
    return Fraction(tick * time_base.numerator, time_base.denominator)


@dataclass(frozen=True, slots=True)
class ReplayedPresentationMap:
    """One original root and its independently replayed, unflattened map.

    This is a mathematical domain, not a persistence capability. Quantization
    bounds and calibration tolerances never enlarge its exact covered domain.
    """

    root: RootMediaEvidenceBundle
    probe: PresentationTimelineProbe
    certificate: CommittedVideoToAudioClockMapCertificate
    source_manifest_sha256: str
    audio_snap_calibration: CalibrationBinding

    def __post_init__(self) -> None:
        if (type(self.root) is not RootMediaEvidenceBundle  # noqa: E721
                or type(self.probe) is not PresentationTimelineProbe  # noqa: E721
                or type(self.certificate) is not CommittedVideoToAudioClockMapCertificate  # noqa: E721
                or type(self.audio_snap_calibration) is not CalibrationBinding):  # noqa: E721
            raise PresentationMapValidationError("presentation map requires exact evidence values")
        try:
            self.certificate.assert_replays_probe(
                self.probe, self.root,
                source_manifest_sha256=self.source_manifest_sha256,
                calibration_binding=self.audio_snap_calibration,
            )
        except ValueError as error:
            raise PresentationMapValidationError("presentation certificate does not replay") from error

    def _require_video_boundary(self, tick: int, *, allow_end: bool) -> None:
        require_pts(tick, "video boundary")
        index = self.root.frame_pts_index
        if not index.pts_index.contains(tick) and not (allow_end and tick == index.context.end_tick):
            raise PresentationMapValidationError("video boundary is not a decoded frame or proven end")

    def _require_audio_boundary(self, tick: int) -> None:
        require_pts(tick, "audio boundary")
        points = self.root.audio_sample_boundaries.points
        ordinal = bisect_left(points, tick, key=lambda point: point.tick)
        if ordinal == len(points) or points[ordinal].tick != tick:
            raise PresentationMapValidationError("audio boundary is not a decoded sample boundary")

    def map_video_tick_bounds(self, video_tick: int) -> tuple[int, int]:
        """Map a frame/proven end to inclusive floor/ceil audio clock ticks.

        These two integers are rounding bounds, not necessarily decoded sample
        endpoints. Call require_av_span_covered for the actual selected pair.
        """
        self._require_video_boundary(video_tick, allow_end=True)
        presentation = _presentation(video_tick, self.probe.video.time_base)
        if not any(interval.start <= presentation <= interval.end
                   for interval in self.certificate.common_presentation_intervals):
            raise PresentationMapValidationError("video boundary is outside common presentation coverage")
        base = self.probe.audio.time_base
        audio_tick = presentation / Fraction(base.numerator, base.denominator)
        return (
            audio_tick.numerator // audio_tick.denominator,
            -((-audio_tick.numerator) // audio_tick.denominator),
        )

    def require_av_span_covered(self, video: TickRange, audio: TickRange) -> int:
        """Return the single continuous segment covering both full A/V spans.

        This checks exact membership/coverage, not synchronization tolerance,
        dialogue, subtitle or visual safety. The compiler checks those too.
        Integer segment ranges are outward envelopes; only rational presentation
        intervals decide coverage, including at negative or fractional PTS.
        """
        if type(video) is not TickRange or type(audio) is not TickRange:  # noqa: E721
            raise PresentationMapValidationError("presentation spans require exact tick ranges")
        self._require_video_boundary(video.start_pts, allow_end=False)
        self._require_video_boundary(video.end_pts, allow_end=True)
        self._require_audio_boundary(audio.start_pts)
        self._require_audio_boundary(audio.end_pts)
        start = min(_presentation(video.start_pts, self.probe.video.time_base),
                    _presentation(audio.start_pts, self.probe.audio.time_base))
        end = max(_presentation(video.end_pts, self.probe.video.time_base),
                  _presentation(audio.end_pts, self.probe.audio.time_base))
        for ordinal, interval in enumerate(self.certificate.common_presentation_intervals):
            if interval.start <= start and end <= interval.end:
                return ordinal
        raise PresentationMapValidationError("A/V span crosses a gap or single-stream non-overlap")
