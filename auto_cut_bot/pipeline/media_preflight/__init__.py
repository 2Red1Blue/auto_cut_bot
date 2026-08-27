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
    RuntimeMediaPreflightRequest,
    RuntimeMediaPreflightResult,
    ToolInvocationTrace,
    ToolTrace,
)
from .port import LocalMediaPreflightPort
from .process import BoundedSubprocessRunner, CommandOutput, CommandRunner
from .runtime_measurement_http import (
    FunASRRuntimeMeasurementIdentityHttpPort,
    RuntimeMeasurementIdentityPort,
)
from .runtime_policy import PcCudaRuntimeTimedSpeechPolicy
from .runtime_speech import RuntimeTimedSpeechEvidenceRequest
from .speech_port import (
    TimedSpeechEvidence,
    TimedSpeechEvidencePort,
    TimedSpeechEvidenceRequest,
    TimedSpeechExpectedProducer,
    TimedSpeechInvocationTrace,
    TimedSpeechProducerIdentity,
    TimedSpeechTimingErrorBound,
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
    "FunASRRuntimeMeasurementIdentityHttpPort",
    "TimedSpeechEvidence",
    "RuntimeMeasurementIdentityPort",
    "TimedSpeechEvidencePort",
    "TimedSpeechEvidenceRequest",
    "TimedSpeechExpectedProducer",
    "TimedSpeechInvocationTrace",
    "TimedSpeechProducerIdentity",
    "TimedSpeechTimingErrorBound",
    "LocalMediaPreflightRequest",
    "LocalMediaPreflightResult",
    "RuntimeMediaPreflightRequest",
    "RuntimeMediaPreflightResult",
    "LocalMediaSourceError",
    "LocalMediaToolError",
    "ProducerCalibrationIdentity",
    "ProducerIdentity",
    "ProducerKind",
    "ToolInvocationTrace",
    "ToolTrace",
    "PcCudaRuntimeTimedSpeechPolicy",
    "RuntimeTimedSpeechEvidenceRequest",
]
