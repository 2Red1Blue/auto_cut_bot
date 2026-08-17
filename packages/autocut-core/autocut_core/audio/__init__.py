"""Audio analysis module: VAD, speech detection, ASR anchor, and future SAM Audio support."""

from autocut_core.audio.vad import (
    SpeechDetector,
    DemucsSileroDetector,
    SpeechInterval,
    VADResult,
    get_vad_detector,
)
from autocut_core.audio.asr_anchor import (
    ASRAnchorDetector,
    AudioAnchorResult,
    WordTimestamp,
    UtteranceBoundary,
    three_tier_snap_start,
    three_tier_snap_end,
    result_from_dict,
    result_to_dict,
)

__all__ = [
    "SpeechDetector",
    "DemucsSileroDetector",
    "ASRAnchorDetector",
    "SpeechInterval",
    "VADResult",
    "AudioAnchorResult",
    "WordTimestamp",
    "UtteranceBoundary",
    "three_tier_snap_start",
    "three_tier_snap_end",
    "result_from_dict",
    "result_to_dict",
    "get_vad_detector",
]
