"""Real, fail-closed local timed-media evidence producers."""

from .funasr_http import FunASRHttpTimedSpeechEvidencePort
from .models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaPreflightError,
    LocalMediaPreflightPolicy,
    LocalMediaPreflightRequest,
    LocalMediaPreflightResult,
    LocalMediaSourceError,
    LocalMediaToolError,
    ProducerCalibrationIdentity,
    ProducerIdentity,
    ProducerKind,
    ToolInvocationTrace,
    ToolTrace,
)
from .port import LocalMediaPreflightPort
from .process import BoundedSubprocessRunner, CommandOutput, CommandRunner
from .speech_port import (
    TimedSpeechEvidence,
    TimedSpeechEvidencePort,
    TimedSpeechEvidenceRequest,
    TimedSpeechExpectedProducer,
    TimedSpeechInvocationTrace,
    TimedSpeechProducerIdentity,
)

__all__ = [
    "BoundedSubprocessRunner",
    "CommandOutput",
    "CommandRunner",
    "LocalMediaEvidenceError",
    "LocalMediaPolicyError",
    "LocalMediaPreflightError",
    "LocalMediaPreflightPolicy",
    "LocalMediaPreflightPort",
    "FunASRHttpTimedSpeechEvidencePort",
    "TimedSpeechEvidence",
    "TimedSpeechEvidencePort",
    "TimedSpeechEvidenceRequest",
    "TimedSpeechExpectedProducer",
    "TimedSpeechInvocationTrace",
    "TimedSpeechProducerIdentity",
    "LocalMediaPreflightRequest",
    "LocalMediaPreflightResult",
    "LocalMediaSourceError",
    "LocalMediaToolError",
    "ProducerCalibrationIdentity",
    "ProducerIdentity",
    "ProducerKind",
    "ToolInvocationTrace",
    "ToolTrace",
]
