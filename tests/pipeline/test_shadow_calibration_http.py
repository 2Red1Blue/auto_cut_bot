from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.media import (
    ShadowCalibrationInvocation,
    shadow_calibration_anchor_reference_sha256,
)
from autocut_kernel.pipeline import (
    MeasureShadowCalibrationCommand,
    MeasureShadowCalibrationRequest,
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
from autocut_kernel.store.models import MaterializationError, MaterializationLimits

from auto_cut_bot.pipeline.media_preflight.models import LocalMediaToolError
from auto_cut_bot.pipeline.media_preflight.shadow_calibration_http import (
    ShadowCalibrationHttpMeasurementPort,
    ShadowCalibrationSourceBinding,
)
from tests.pipeline.test_measure_shadow_calibration_command import (
    _projection,
    _raw,
    _request,
    _sha,
    _Store,
    _two_member_request,
)

ENDPOINT = "http://127.0.0.1:8765/v1/shadow-calibration-funasr-raw"
LIMITS = MaterializationLimits(8192, 4096, 512, 16384)


@dataclass
class Lease:
    reference: BlobRef
    path: Path
    closes: int = 0

    def close(self) -> None:
        self.closes += 1
        self.path.unlink(missing_ok=True)


@dataclass
class Store:
    directory: Path
    calls: list[tuple[Job, BlobRef, MaterializationLimits]] = field(default_factory=list)
    leases: list[Lease] = field(default_factory=list)
    failure: Exception | None = None
    drift: str = ""

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> Lease:
        self.calls.append((job, reference, limits))
        if self.failure:
            raise self.failure
        path = self.directory / f"lease-{len(self.calls)}.mp4"
        if self.drift != "missing":
            path.write_bytes(b"s" * (reference.byte_length - (self.drift == "size")))
        if self.drift == "symlink":
            target = self.directory / "target.mp4"
            path.rename(target)
            path.symlink_to(target)
        lease = Lease(
            replace(reference, object_id=uuid4()) if self.drift == "reference" else reference, path
        )
        self.leases.append(lease)
        return lease


@dataclass
class Transport:
    raw: bytes
    status: int = 200
    failure: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body_path: Path,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "body_path": body_path,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
                "body": body_path.read_bytes(),
            }
        )
        if self.failure:
            raise self.failure
        return self.status, self.raw


def _fixture(
    tmp_path: Path, *, two: bool = False
) -> tuple[MeasureShadowCalibrationRequest, Store, Transport, dict[str, Any]]:
    request = _two_member_request() if two else _request()
    members = []
    bindings = []
    for index, member in enumerate(request.corpus_members):
        source = replace(
            member.raw_context.source,
            source_sha256=sha256_bytes(b"s" * 4096),
            blob_sha256=sha256_bytes(b"s" * 4096),
        )
        context = replace(member.raw_context, source=source)
        mapping = replace(member.native_invocation.request_mapping, source=source)
        invocation = ShadowCalibrationInvocation(
            source.corpus_member_reference_sha256, mapping.sha256, mapping, mapping.sha256
        )
        members.append(
            replace(
                member,
                raw_context=context,
                native_invocation=invocation,
                expected_anchor_reference_sha256=shadow_calibration_anchor_reference_sha256(
                    context
                ),
            )
        )
        bindings.append(
            ShadowCalibrationSourceBinding(
                source.corpus_member_reference_sha256,
                Job(f"original-source-owner-{index}", "test"),
                BlobRef(
                    UUID(source.blob_id),
                    source.blob_sha256,
                    source.blob_byte_length,
                    source.blob_media_type,
                ),
            )
        )
    request = replace(request, corpus_members=tuple(members))
    store = Store(tmp_path)
    transport = Transport(_raw(request.corpus_members[0].native_invocation))
    return (
        request,
        store,
        transport,
        {
            "expected_request": request,
            "source_bindings": tuple(bindings),
            "store": store,
            "limits": LIMITS,
            "endpoint_url": ENDPOINT,
            "shared_token": "explicit-test-token",
            "timeout_seconds": 7,
            "max_response_bytes": 65536,
            "transport": transport,
        },
    )


def _assert_rejected(error: pytest.ExceptionInfo[ShadowCalibrationProducerError]) -> None:
    assert error.value.code is ShadowCalibrationProducerFailureCode.REJECTED


def test_single_dispatch_uses_original_owner_and_exact_canonical_manifest(tmp_path: Path) -> None:
    request, store, transport, options = _fixture(tmp_path)
    port = ShadowCalibrationHttpMeasurementPort(**options)
    member = request.corpus_members[0]
    result = port.measure(replace(request), replace(member))
    assert result.invocation == member.native_invocation
    assert result.projection == _projection(member.native_invocation)
    assert result.raw_blob.raw == transport.raw
    assert result.raw_blob.content_sha256 == sha256_bytes(transport.raw)
    assert len(transport.calls) == len(store.calls) == 1
    binding = options["source_bindings"][0]
    assert store.calls == [(binding.owner_job, binding.source_blob, LIMITS)]
    assert binding.owner_job != request.job
    sent = transport.calls[0]
    assert sent["headers"] == {
        "Content-Type": "application/octet-stream",
        "X-Shadow-Calibration-Manifest": base64.b64encode(
            canonical_json_bytes(member.native_invocation.request_mapping.to_mapping())
        ).decode(),
        "X-Shadow-Calibration-Request-SHA256": member.native_invocation.request_identity_sha256,
        "Authorization": "Bearer explicit-test-token",
    }
    assert sent["url"] == ENDPOINT and sent["timeout_seconds"] == 7
    assert sent["max_response_bytes"] == member.native_invocation.request_mapping.max_response_bytes
    assert sent["body"] == b"s" * 4096
    assert store.leases[0].closes == 1 and not sent["body_path"].exists()


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://127.0.0.1:8765/v1/shadow-calibration-funasr-raw",
        "http://localhost:8765/v1/shadow-calibration-funasr-raw",
        "http://127.0.0.2:8765/v1/shadow-calibration-funasr-raw",
        "http://user:pass@127.0.0.1:8765/v1/shadow-calibration-funasr-raw",
        "http://127.0.0.1/v1/shadow-calibration-funasr-raw",
        "http://127.0.0.1:0/v1/shadow-calibration-funasr-raw",
        "http://127.0.0.1:65536/v1/shadow-calibration-funasr-raw",
        "http://127.0.0.1:8765/v1/timed-speech-evidence",
        ENDPOINT + "?x=1",
        ENDPOINT + "#fragment",
        ENDPOINT + ";params",
        ENDPOINT + "/",
        " " + ENDPOINT,
        ENDPOINT + "?",
        ENDPOINT + "#",
        None,
    ),
)
def test_endpoint_is_exact_loopback_before_materialization(
    tmp_path: Path, endpoint: object
) -> None:
    _, store, transport, options = _fixture(tmp_path)
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**{**options, "endpoint_url": endpoint})
    _assert_rejected(error)
    assert not store.calls and not transport.calls


@pytest.mark.parametrize(
    "field,value",
    (
        ("shared_token", ""),
        ("shared_token", None),
        ("shared_token", "bad\nheader"),
        ("shared_token", "a b"),
        ("shared_token", "密钥"),
        ("timeout_seconds", True),
        ("timeout_seconds", 0),
        ("timeout_seconds", 1.5),
        ("max_response_bytes", True),
        ("max_response_bytes", 0),
        ("max_response_bytes", 1.5),
        ("max_response_bytes", 32767),
        ("limits", None),
        ("expected_request", None),
    ),
)
def test_explicit_operational_inputs_fail_closed(
    tmp_path: Path, field: str, value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, transport, options = _fixture(tmp_path)
    monkeypatch.setenv("FUNASR_SHARED_TOKEN", "must-not-fallback")
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**{**options, field: value})
    _assert_rejected(error)
    assert not store.calls and not transport.calls


@pytest.mark.parametrize(
    "drift", ("empty", "duplicate", "missing", "extra", "blob", "hash", "size", "media", "limits")
)
def test_entire_source_binding_set_closes_before_any_io(tmp_path: Path, drift: str) -> None:
    _, store, transport, options = _fixture(tmp_path, two=True)
    bindings = options["source_bindings"]
    if drift == "empty":
        options["source_bindings"] = ()
    elif drift == "duplicate":
        options["source_bindings"] = (bindings[0], bindings[0])
    elif drift == "missing":
        options["source_bindings"] = bindings[:1]
    elif drift == "extra":
        options["source_bindings"] = (
            *bindings,
            replace(bindings[1], corpus_member_reference_sha256=_sha(999)),
        )
    elif drift == "limits":
        options["limits"] = replace(LIMITS, max_source_bytes=8193)
    else:
        fields = {
            "blob": {"object_id": uuid4()},
            "hash": {"content_hash": _sha(999)},
            "size": {"byte_length": 4097},
            "media": {"media_type": "audio/wav"},
        }
        options["source_bindings"] = (
            bindings[0],
            replace(bindings[1], source_blob=replace(bindings[1].source_blob, **fields[drift])),
        )
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**options)
    _assert_rejected(error)
    assert not store.calls and not transport.calls


@pytest.mark.parametrize("drift", ("request", "member", "foreign-member", "wrong-type"))
def test_measure_rejects_request_or_member_substitution_before_io(
    tmp_path: Path, drift: str
) -> None:
    request, store, transport, options = _fixture(tmp_path)
    port = ShadowCalibrationHttpMeasurementPort(**options)
    member = request.corpus_members[0]
    if drift == "request":
        request = replace(
            request,
            shadow_inputs=replace(request.shadow_inputs, registry_snapshot_sha256=_sha(999)),
        )
    elif drift == "member":
        member = replace(member, expected_anchor_reference_sha256=_sha(999))
    elif drift == "foreign-member":
        member = _two_member_request().corpus_members[1]
    else:
        request = None  # type: ignore[assignment]
    with pytest.raises(ShadowCalibrationProducerError) as error:
        port.measure(request, member)
    _assert_rejected(error)
    assert not store.calls and not transport.calls


@pytest.mark.parametrize("drift", ("reference", "size", "symlink", "missing"))
def test_lease_is_verified_and_closed_before_any_dispatch(tmp_path: Path, drift: str) -> None:
    request, store, transport, options = _fixture(tmp_path)
    store.drift = drift
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**options).measure(request, request.corpus_members[0])
    expected = (
        ShadowCalibrationProducerFailureCode.UNAVAILABLE
        if drift == "missing"
        else ShadowCalibrationProducerFailureCode.REJECTED
    )
    assert error.value.code is expected
    assert not transport.calls and store.leases[0].closes == 1


@pytest.mark.parametrize(
    "failure,expected",
    (
        (
            MaterializationError("MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED", "too large", outcome="denied"),
            "REJECTED",
        ),
        (
            MaterializationError("MEDIA_MATERIALIZATION_CAPACITY_BUSY", "busy", outcome="failed"),
            "UNAVAILABLE",
        ),
        (BlobIntegrityError("owner mismatch"), "REJECTED"),
        (StoreValidationError("bad reference"), "REJECTED"),
        (RuntimeStoreError("offline"), "UNAVAILABLE"),
        (OSError("disk"), "UNAVAILABLE"),
    ),
)
def test_materialization_outcomes_never_dispatch(
    tmp_path: Path, failure: Exception, expected: str
) -> None:
    request, store, transport, options = _fixture(tmp_path)
    store.failure = failure
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**options).measure(request, request.corpus_members[0])
    assert error.value.code is ShadowCalibrationProducerFailureCode[expected]
    assert len(store.calls) == 1 and not transport.calls and not store.leases


@pytest.mark.parametrize(
    "failure", (TimeoutError(), ConnectionError(), LocalMediaToolError("unknown"))
)
def test_transport_failure_is_unavailable_without_retry_and_cleans_up(
    tmp_path: Path, failure: Exception
) -> None:
    request, store, transport, options = _fixture(tmp_path)
    transport.failure = failure
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**options).measure(request, request.corpus_members[0])
    assert error.value.code is ShadowCalibrationProducerFailureCode.UNAVAILABLE
    assert len(transport.calls) == 1 and store.leases[0].closes == 1


@pytest.mark.parametrize("status", (301, 400, 401, 413, 500, 503))
def test_non_200_is_unavailable_without_retry(tmp_path: Path, status: int) -> None:
    request, store, transport, options = _fixture(tmp_path)
    transport.status = status
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**options).measure(request, request.corpus_members[0])
    assert error.value.code is ShadowCalibrationProducerFailureCode.UNAVAILABLE
    assert len(transport.calls) == 1 and store.leases[0].closes == 1


@pytest.mark.parametrize(
    "drift", ("invalid-json", "duplicate", "float", "identity", "word-time", "bound")
)
def test_invalid_raw_response_rejected_by_real_decoder_and_cleanup(
    tmp_path: Path, drift: str
) -> None:
    request, store, transport, options = _fixture(tmp_path)
    response = json.loads(transport.raw)
    if drift == "invalid-json":
        transport.raw = b"not json"
    elif drift == "duplicate":
        transport.raw = b'{"schema_version":"wrong",' + transport.raw[1:]
    elif drift == "bound":
        transport.raw = b" " * 32769
    else:
        if drift == "float":
            response["asr_native_output"][0]["timestamp"][0][0] = 100.0
        elif drift == "identity":
            response["request_identity_sha256"] = _sha(999)
        else:
            response["asr_native_output"][0]["timestamp"] = []
        transport.raw = json.dumps(response).encode()
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**options).measure(request, request.corpus_members[0])
    _assert_rejected(error)
    assert len(transport.calls) == 1 and store.leases[0].closes == 1


def test_cancellation_also_closes_lease_without_retry(tmp_path: Path) -> None:
    request, store, transport, options = _fixture(tmp_path)
    transport.failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        ShadowCalibrationHttpMeasurementPort(**options).measure(request, request.corpus_members[0])
    assert len(transport.calls) == 1 and store.leases[0].closes == 1


def test_second_member_uses_its_original_owner_without_dispatching_first(tmp_path: Path) -> None:
    request, store, transport, options = _fixture(tmp_path, two=True)
    member = request.corpus_members[1]
    transport.raw = _raw(member.native_invocation)
    result = ShadowCalibrationHttpMeasurementPort(**options).measure(request, member)
    binding = options["source_bindings"][1]
    assert store.calls == [(binding.owner_job, binding.source_blob, LIMITS)]
    assert result.invocation == member.native_invocation
    assert len(transport.calls) == 1 and store.leases[0].closes == 1


def test_source_exceeding_frozen_ceiling_is_rejected_before_materialization(tmp_path: Path) -> None:
    request, store, transport, options = _fixture(tmp_path)
    member = request.corpus_members[0]
    source = replace(member.raw_context.source, blob_byte_length=4097)
    context = replace(member.raw_context, source=source)
    mapping = replace(member.native_invocation.request_mapping, source=source)
    invocation = ShadowCalibrationInvocation(
        source.corpus_member_reference_sha256, mapping.sha256, mapping, mapping.sha256
    )
    options["expected_request"] = replace(
        request,
        corpus_members=(replace(member, raw_context=context, native_invocation=invocation),),
    )
    binding = options["source_bindings"][0]
    options["source_bindings"] = (
        replace(binding, source_blob=replace(binding.source_blob, byte_length=4097)),
    )
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**options)
    _assert_rejected(error)
    assert not store.calls and not transport.calls


@pytest.mark.parametrize(
    "field,value",
    (
        ("corpus_member_reference_sha256", "sha256:" + "0" * 64),
        ("corpus_member_reference_sha256", "not-a-hash"),
        ("owner_job", "path-or-guessed-owner"),
        ("source_blob", "caller/path.mp4"),
    ),
)
def test_source_bindings_are_exact_typed_inputs(tmp_path: Path, field: str, value: object) -> None:
    _, store, transport, options = _fixture(tmp_path)
    with pytest.raises(ShadowCalibrationProducerError) as error:
        replace(options["source_bindings"][0], **{field: value})
    _assert_rejected(error)
    assert not store.calls and not transport.calls


@pytest.mark.parametrize("lost_stage_response", (False, True))
def test_measurement_command_replay_never_dispatches_or_materializes_twice(
    tmp_path: Path, lost_stage_response: bool
) -> None:
    request, materializer, transport, options = _fixture(tmp_path)
    durable = _Store(stage_timeout_after_commit=lost_stage_response)
    command = MeasureShadowCalibrationCommand(
        durable, ShadowCalibrationHttpMeasurementPort(**options)
    )
    if lost_stage_response:
        with pytest.raises(TimeoutError, match="stage commit response was lost"):
            command.execute(request)
        assert durable.initial_attempt_id is not None
        attempt = durable.attempts[durable.initial_attempt_id]
        assert attempt.state == "ready" and attempt.members[0].state == "staged"
    else:
        assert command.execute(request).state == "succeeded"
    assert len(transport.calls) == len(materializer.calls) == 1
    assert materializer.leases[0].closes == 1

    # Reconstruct the adapter/command, retaining only the Store's durable state.
    resumed = MeasureShadowCalibrationCommand(
        durable, ShadowCalibrationHttpMeasurementPort(**options)
    )
    accepted = resumed.execute(request)
    assert accepted.state == "succeeded"
    assert resumed.execute(request) == accepted
    assert len(transport.calls) == len(materializer.calls) == 1
    assert durable.finalizations == 1 and len(durable.blobs) == 1


@pytest.mark.parametrize("response_limit", (32768, 2**53 + 1))
def test_unicode_source_wire_hash_matches_media_identity_without_extra_integer_limit(
    tmp_path: Path, response_limit: int
) -> None:
    request, store, transport, options = _fixture(tmp_path)
    original = request.corpus_members[0]
    source = replace(original.raw_context.source, source_id="校准语料-第一集")
    context = replace(original.raw_context, source=source)
    mapping = replace(
        original.native_invocation.request_mapping, source=source, max_response_bytes=response_limit
    )
    invocation = ShadowCalibrationInvocation(
        source.corpus_member_reference_sha256, mapping.sha256, mapping, mapping.sha256
    )
    member = replace(original, raw_context=context, native_invocation=invocation)
    request = replace(request, corpus_members=(member,))
    # Source ID is semantic request metadata; its exact owner/BlobRef is unchanged.
    options.update(expected_request=request, max_response_bytes=response_limit)
    transport.raw = _raw(invocation)
    result = ShadowCalibrationHttpMeasurementPort(**options).measure(request, member)

    headers = transport.calls[0]["headers"]
    manifest = base64.b64decode(headers["X-Shadow-Calibration-Manifest"], validate=True)
    assert sha256_bytes(manifest) == invocation.request_identity_sha256
    assert headers["X-Shadow-Calibration-Request-SHA256"] == invocation.request_identity_sha256
    assert b"\\u6821" in manifest and source.source_id.encode("utf-8") not in manifest
    assert json.loads(manifest)["source"]["source_id"] == source.source_id
    assert transport.calls[0]["max_response_bytes"] == response_limit
    assert result.projection == _projection(invocation)
    assert result.raw_blob.raw == transport.raw
    assert len(transport.calls) == len(store.calls) == store.leases[0].closes == 1


def test_derived_measurement_job_cannot_impersonate_original_source_owner(tmp_path: Path) -> None:
    request, store, transport, options = _fixture(tmp_path)
    binding = options["source_bindings"][0]
    options["source_bindings"] = (replace(binding, owner_job=request.job),)
    with pytest.raises(ShadowCalibrationProducerError) as error:
        ShadowCalibrationHttpMeasurementPort(**options)
    _assert_rejected(error)
    assert not store.calls and not transport.calls
