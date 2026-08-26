"""Single-dispatch shadow transport; never authority or native-retry ownership.

Source-owner bindings are deployment inputs. A future controlled composition
resolver must prove their committed-source provenance; this adapter only checks
their exact identities and uses the Store's verified, owner-bound file lease.
"""

from __future__ import annotations

import base64
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID

from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.media import (
    SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
    ShadowCalibrationRawBlob,
    derive_shadow_calibration_raw_response,
)
from autocut_kernel.media.types import sha256_prefixed
from autocut_kernel.pipeline.measure_shadow_calibration_command import (
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationPortResult,
    ShadowCalibrationProducerError,
    ShadowCalibrationProducerFailureCode,
)
from autocut_kernel.store import (
    BlobIntegrityError,
    BlobRef,
    Job,
    RuntimeStoreError,
    StoreValidationError,
)
from autocut_kernel.store.models import (
    MaterializationError,
    MaterializationLimits,
    VerifiedMaterializedBlob,
)

from .http_transport import FileHttpTransport, HttpxFileTransport
from .models import LocalMediaToolError


def _rejected() -> ShadowCalibrationProducerError:
    return ShadowCalibrationProducerError(ShadowCalibrationProducerFailureCode.REJECTED)


def _canonical_json_bytes(payload: object) -> bytes:
    """Use the media/service protocol encoding, not the compiler JSON subset."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ShadowCalibrationSourceBinding:
    corpus_member_reference_sha256: str
    owner_job: Job
    source_blob: BlobRef

    def __post_init__(self) -> None:
        try:
            sha256_prefixed(self.corpus_member_reference_sha256, "corpus member reference")
        except ValueError as error:
            raise _rejected() from error
        if (
            self.corpus_member_reference_sha256 == "sha256:" + "0" * 64
            or type(self.owner_job) is not Job
            or type(self.source_blob) is not BlobRef
        ):
            raise _rejected()


class ShadowCalibrationMaterializationStore(Protocol):
    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> VerifiedMaterializedBlob: ...


class ShadowCalibrationHttpMeasurementPort:
    def __init__(
        self,
        *,
        expected_request: MeasureShadowCalibrationRequest,
        source_bindings: tuple[ShadowCalibrationSourceBinding, ...],
        store: ShadowCalibrationMaterializationStore,
        limits: MaterializationLimits,
        endpoint_url: str,
        shared_token: str,
        timeout_seconds: int,
        max_response_bytes: int,
        transport: FileHttpTransport | None = None,
    ) -> None:
        if (
            type(expected_request) is not MeasureShadowCalibrationRequest
            or type(limits) is not MaterializationLimits
            or type(source_bindings) is not tuple
            or any(type(item) is not ShadowCalibrationSourceBinding for item in source_bindings)
            or type(timeout_seconds) is not int
            or timeout_seconds <= 0
            or type(max_response_bytes) is not int
            or max_response_bytes <= 0
            or type(shared_token) is not str
            or not shared_token
            or any(not 33 <= ord(character) <= 126 for character in shared_token)
        ):
            raise _rejected()
        try:
            parsed = urlparse(endpoint_url)
            port = parsed.port
            if (
                type(endpoint_url) is not str
                or port is None
                or not 1 <= port <= 65535
                or endpoint_url != f"http://127.0.0.1:{port}/v1/shadow-calibration-funasr-raw"
            ):
                raise _rejected()
        except (ValueError, TypeError, AttributeError) as error:
            raise _rejected() from error
        bindings = {item.corpus_member_reference_sha256: item for item in source_bindings}
        if len(bindings) != len(source_bindings) or set(bindings) != {
            item.corpus_member_reference_sha256 for item in expected_request.corpus_members
        }:
            raise _rejected()
        for member in expected_request.corpus_members:
            source, source_limits = member.raw_context.source, member.raw_context.source_byte_limits
            binding = bindings[member.corpus_member_reference_sha256]
            if (
                binding.owner_job == expected_request.job
                or binding.source_blob
                != BlobRef(
                    UUID(source.blob_id),
                    source.blob_sha256,
                    source.blob_byte_length,
                    source.blob_media_type,
                )
                or (
                    limits.max_source_bytes,
                    limits.timed_speech_max_request_bytes,
                    limits.effective_max_source_bytes,
                )
                != (
                    source_limits.kernel_max_source_bytes,
                    source_limits.service_max_request_bytes,
                    source_limits.effective_max_source_bytes,
                )
                or source.blob_byte_length > limits.effective_max_source_bytes
                or member.native_invocation.request_mapping.max_response_bytes > max_response_bytes
            ):
                raise _rejected()
        self._expected_request = expected_request
        self._expected_bytes = _canonical_json_bytes(expected_request.canonical_payload())
        self._bindings = bindings
        self._store, self._limits = store, limits
        self._endpoint, self._token = endpoint_url, shared_token
        self._timeout = timeout_seconds
        self._transport = transport if transport is not None else HttpxFileTransport()

    def measure(
        self, request: MeasureShadowCalibrationRequest, member: ShadowCalibrationCorpusMember
    ) -> ShadowCalibrationPortResult:
        try:
            if (
                type(request) is not MeasureShadowCalibrationRequest
                or type(member) is not ShadowCalibrationCorpusMember
                or request.request_hash != self._expected_request.request_hash
                or _canonical_json_bytes(request.canonical_payload()) != self._expected_bytes
            ):
                raise _rejected()
            matches = tuple(
                item
                for item in self._expected_request.corpus_members
                if item.corpus_member_reference_sha256 == member.corpus_member_reference_sha256
            )
            if len(matches) != 1 or _canonical_json_bytes(
                MeasureShadowCalibrationRequest(
                    request.shadow_inputs, (member,)
                ).canonical_payload()
            ) != _canonical_json_bytes(
                MeasureShadowCalibrationRequest(request.shadow_inputs, matches).canonical_payload()
            ):
                raise _rejected()
            binding = self._bindings[member.corpus_member_reference_sha256]
            lease = self._store.materialize_immutable_blob(
                binding.owner_job, binding.source_blob, self._limits
            )
            try:
                if type(lease.reference) is not BlobRef or lease.reference != binding.source_blob:
                    raise _rejected()
                if not isinstance(lease.path, Path):  # pyright: ignore[reportUnnecessaryIsInstance]
                    raise _rejected()
                metadata = lease.path.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != binding.source_blob.byte_length
                ):
                    raise _rejected()
                invocation = member.native_invocation
                mapping = invocation.request_mapping
                status, raw = self._transport.post(
                    self._endpoint,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Shadow-Calibration-Manifest": base64.b64encode(
                            _canonical_json_bytes(mapping.to_mapping())
                        ).decode("ascii"),
                        "X-Shadow-Calibration-Request-SHA256": invocation.request_identity_sha256,
                        "Authorization": f"Bearer {self._token}",
                    },
                    body_path=lease.path,
                    timeout_seconds=self._timeout,
                    max_response_bytes=mapping.max_response_bytes,
                )
                if type(status) is not int or type(raw) is not bytes:
                    raise _rejected()
                if status != 200:
                    raise ShadowCalibrationProducerError(
                        ShadowCalibrationProducerFailureCode.UNAVAILABLE
                    )
                if len(raw) > mapping.max_response_bytes:
                    raise _rejected()
                blob = ShadowCalibrationRawBlob(
                    raw, SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE, len(raw), sha256_bytes(raw)
                )
                projection = derive_shadow_calibration_raw_response(
                    blob, invocation, member.raw_context
                ).projection
                return ShadowCalibrationPortResult(invocation, blob, projection)
            finally:
                lease.close()
        except MaterializationError as error:
            code = (
                ShadowCalibrationProducerFailureCode.REJECTED
                if error.outcome == "denied"
                else ShadowCalibrationProducerFailureCode.UNAVAILABLE
            )
            raise ShadowCalibrationProducerError(code) from error
        except (BlobIntegrityError, StoreValidationError, ValueError) as error:
            raise _rejected() from error
        except (RuntimeStoreError, OSError, LocalMediaToolError) as error:
            raise ShadowCalibrationProducerError(
                ShadowCalibrationProducerFailureCode.UNAVAILABLE
            ) from error
