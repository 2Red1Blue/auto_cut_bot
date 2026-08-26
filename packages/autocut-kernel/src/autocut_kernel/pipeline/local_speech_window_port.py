"""Application-independent single-dispatch speech port, without retry authority."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..media.local_speech_window import LocalSpeechWindowRequest
from ..media.local_speech_window_busy import LocalSpeechWindowBusyProof
from ..media.local_speech_window_projection import LocalSpeechWindowEvidence


@dataclass(frozen=True, slots=True)
class ReceivedLocalSpeechWindow:
    """Producer content only; Command/readers must independently replay raw bytes."""

    evidence: LocalSpeechWindowEvidence
    raw_response: bytes

    def __post_init__(self) -> None:
        if (type(self.evidence) is not LocalSpeechWindowEvidence
                or type(self.raw_response) is not bytes or not self.raw_response):
            raise ValueError("local speech delivery requires typed evidence and immutable raw bytes")


class LocalSpeechWindowPreDispatchBusyError(RuntimeError):
    """Canonical proof carrier, not permission to retry or a committed Receipt.

    The Command must re-decode this proof against its own expected wire request;
    the successor must also re-read the exact terminal predecessor from Store.
    """

    code = "TIMED_SPEECH_BUSY"

    def __init__(self, proof: LocalSpeechWindowBusyProof, raw_response: bytes) -> None:
        if (type(proof) is not LocalSpeechWindowBusyProof or type(raw_response) is not bytes
                or proof.to_bytes() != raw_response):
            raise ValueError("busy evidence must retain exact canonical proof bytes")
        super().__init__("window admission refused before inference started")
        self._proof = proof
        self._raw_response = raw_response

    @property
    def proof(self) -> LocalSpeechWindowBusyProof:
        return self._proof

    @property
    def raw_response(self) -> bytes:
        return self._raw_response


class LocalSpeechWindowProducerPort(Protocol):
    """The caller owns the durable claim and verified private source lease.

    No retry, cleanup or persistence happens here. The existing HTTP adapter
    implements this interface; Kernel never imports its application module.
    """

    def produce(self, source_path: Path, request: LocalSpeechWindowRequest) -> ReceivedLocalSpeechWindow: ...
