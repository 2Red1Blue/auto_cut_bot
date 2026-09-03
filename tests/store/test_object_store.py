from __future__ import annotations

import base64
import hashlib
import os
import traceback
from collections.abc import Callable, Mapping
from dataclasses import fields, replace
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

import pytest
from autocut_kernel.store.object_store import (
    OBJECT_STORE_WRITE_STRATEGY,
    ObjectStoreReadError,
    ObjectStoreReadLimits,
    ObjectStoreWriteError,
    ObjectStoreWriteLimits,
    PendingObjectIntent,
    S3ObjectStoreConfig,
    S3PendingObjectStore,
    _issue_pending_object_reservation,  # pyright: ignore[reportPrivateUsage]
    _issue_s3_read_grant,  # pyright: ignore[reportPrivateUsage]
    _PendingObjectReservation,  # pyright: ignore[reportPrivateUsage]
    _PendingObjectTarget,  # pyright: ignore[reportPrivateUsage]
    _S3ReadGrant,  # pyright: ignore[reportPrivateUsage]
)


class _ProviderError(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}
        super().__init__("provider detail that must never escape")


class _FakeBody:
    def __init__(
        self,
        content: bytes,
        *,
        after_exhausted: Callable[[], None] | None = None,
        violate_read_bound: bool = False,
    ) -> None:
        self._content = content
        self._offset = 0
        self._after_exhausted = after_exhausted
        self._did_after_exhausted = False
        self._violate_read_bound = violate_read_bound
        self.maximum_requested = 0
        self.closed = False

    def read(self, size: int) -> bytes:
        self.maximum_requested = max(self.maximum_requested, size)
        if self._offset >= len(self._content):
            if not self._did_after_exhausted and self._after_exhausted is not None:
                self._did_after_exhausted = True
                self._after_exhausted()
            return b""
        actual_size = size + 1 if self._violate_read_bound else size
        chunk = self._content[self._offset : self._offset + actual_size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class _CloseOnlyBody:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RaisingMetadataMapping(Mapping[str, object]):
    def __init__(self, body: _FakeBody) -> None:
        self.body = body

    def __getitem__(self, key: str) -> object:
        if key == "Body":
            return self.body
        raise RuntimeError("malformed provider mapping")

    def __iter__(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("malformed provider mapping")

    def __len__(self) -> int:
        return 1

    def get(self, key: str, default: object = None) -> object:
        if key == "Body":
            return self.body
        raise RuntimeError("malformed provider mapping")


class _BodyCarrier:
    def __init__(self, body: _FakeBody) -> None:
        self.Body = body


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.put_calls: list[dict[str, object]] = []
        self.head_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.put_error: Exception | None = None
        self.head_error: Exception | None = None
        self.get_error: Exception | None = None
        self.get_overrides: dict[str, object] = {}
        self.get_after_exhausted: Callable[[], None] | None = None
        self.get_violates_read_bound = False
        self.head_response_override: object | None = None
        self.get_body_override: object | None = None
        self.last_body: _FakeBody | None = None
        self.after_read: Callable[[], None] | None = None
        self.maximum_read_size = 0

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        self.put_calls.append(dict(kwargs))
        body = cast(BinaryIO, kwargs["Body"])
        assert not isinstance(body, (bytes, bytearray, memoryview))
        content = bytearray()
        while chunk := body.read(3):
            self.maximum_read_size = max(self.maximum_read_size, len(chunk))
            content.extend(chunk)
        if self.after_read is not None:
            self.after_read()
        if self.put_error is not None:
            raise self.put_error
        key = (cast(str, kwargs["Bucket"]), cast(str, kwargs["Key"]))
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _ProviderError("PreconditionFailed")
        self.objects[key] = {
            "bytes": bytes(content),
            "ContentLength": len(content),
            "ContentType": kwargs["ContentType"],
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
            "Metadata": dict(cast(Mapping[str, str], kwargs["Metadata"])),
            "ETag": '"etag-1"',
            "VersionId": "version-1",
        }
        return {
            "ETag": '"etag-1"',
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
            "VersionId": "version-1",
        }

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        self.head_calls.append(dict(kwargs))
        if self.head_error is not None:
            raise self.head_error
        if self.head_response_override is not None:
            return cast(Mapping[str, object], self.head_response_override)
        key = (cast(str, kwargs["Bucket"]), cast(str, kwargs["Key"]))
        try:
            stored = self.objects[key]
        except KeyError as error:
            raise _ProviderError("NoSuchKey") from error
        requested_version = kwargs.get("VersionId")
        if requested_version is not None and requested_version != stored.get("VersionId"):
            raise _ProviderError("NoSuchVersion")
        return {name: value for name, value in stored.items() if name != "bytes"}

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        self.get_calls.append(dict(kwargs))
        if self.get_error is not None:
            raise self.get_error
        key = (cast(str, kwargs["Bucket"]), cast(str, kwargs["Key"]))
        try:
            stored = self.objects[key]
        except KeyError as error:
            raise _ProviderError("NoSuchKey") from error
        requested_version = kwargs.get("VersionId")
        if requested_version is not None and requested_version != stored.get("VersionId"):
            raise _ProviderError("NoSuchVersion")
        if kwargs.get("IfMatch") != stored.get("ETag"):
            raise _ProviderError("PreconditionFailed")
        body = _FakeBody(
            cast(bytes, stored["bytes"]),
            after_exhausted=self.get_after_exhausted,
            violate_read_bound=self.get_violates_read_bound,
        )
        self.last_body = body
        response = {name: value for name, value in stored.items() if name != "bytes"}
        response.update(self.get_overrides)
        response["Body"] = (
            body if self.get_body_override is None else self.get_body_override
        )
        return response


class _RaisingMetadataS3(_FakeS3):
    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        response = super().get_object(**kwargs)
        body = cast(_FakeBody, response["Body"])
        return _RaisingMetadataMapping(body)


class _NonMappingBodyS3(_FakeS3):
    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        response = super().get_object(**kwargs)
        body = cast(_FakeBody, response["Body"])
        return cast(Mapping[str, object], _BodyCarrier(body))


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _reservation_for(intent: PendingObjectIntent) -> _PendingObjectReservation:
    return _issue_pending_object_reservation(
        intent=intent,
        target=_PendingObjectTarget(
            backend_id="workspace-s3",
            storage_region="local-primary",
            storage_locator=(
                f"autocut/workspace/{intent.object_id.hex[:2]}/{intent.object_id.hex}"
            ),
        ),
        job_id=UUID("22222222-2222-4222-8222-222222222222"),
        reservation_token=UUID("33333333-3333-4333-8333-333333333333"),
        expected_version=0,
    )


def _intent(content: bytes) -> _PendingObjectReservation:
    return _reservation_for(
        PendingObjectIntent(
            UUID("11111111-1111-4111-8111-111111111111"),
            _sha256(content),
            len(content),
            "video/mp4",
        )
    )


def _limits(maximum: int = 1024) -> ObjectStoreWriteLimits:
    return ObjectStoreWriteLimits(maximum, min(maximum, 4))


def _attempt_output(tmp_path: Path, content: bytes) -> tuple[Path, Path]:
    attempt = tmp_path / "render-attempt"
    attempt.mkdir(mode=0o700)
    output = attempt / "asset.mp4"
    output.write_bytes(content)
    output.chmod(0o400)
    return attempt.resolve(), output.resolve()


def _store(client: _FakeS3) -> S3PendingObjectStore:
    return S3PendingObjectStore(
        client,
        S3ObjectStoreConfig(
            backend_id="workspace-s3",
            storage_region="local-primary",
            bucket="private-bucket",
            key_prefix="autocut/workspace",
        ),
    )


def _seed_exact(client: _FakeS3, intent: _PendingObjectReservation) -> None:
    checksum = base64.b64encode(bytes.fromhex(intent.content_hash[7:])).decode("ascii")
    key = f"autocut/workspace/{intent.object_id.hex[:2]}/{intent.object_id.hex}"
    client.objects[("private-bucket", key)] = {
        "bytes": b"already-present",
        "ContentLength": intent.byte_length,
        "ContentType": intent.media_type,
        "ChecksumSHA256": checksum,
        "Metadata": {
            "autocut-object-id": str(intent.object_id),
            "autocut-content-sha256": intent.content_hash,
        },
        "ETag": '"existing-etag"',
        "VersionId": "existing-version",
    }


def _read_grant_for(
    intent: _PendingObjectReservation,
    *,
    backend_id: str = "workspace-s3",
    storage_region: str = "local-primary",
    storage_locator: str | None = None,
    etag: str = '"existing-etag"',
    version_id: str | None = "existing-version",
) -> _S3ReadGrant:
    return _issue_s3_read_grant(
        reference=intent.reference,
        backend_id=backend_id,
        storage_region=storage_region,
        storage_locator=storage_locator or intent.target.storage_locator,
        etag=etag,
        version_id=version_id,
        write_strategy=OBJECT_STORE_WRITE_STRATEGY,
    )


def _read_limits(maximum: int = 1024, chunk: int = 4) -> ObjectStoreReadLimits:
    return ObjectStoreReadLimits(maximum, min(maximum, chunk))


def _empty_private_destination(tmp_path: Path) -> tuple[int, Path]:
    path = tmp_path / "private-materialization"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    return descriptor, path


def test_streams_sealed_output_with_conditional_checksum_and_exact_head(
    tmp_path: Path,
) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    client = _FakeS3()

    result = _store(client).put_path(
        _intent(content),
        source_path=output,
        attempt_directory=attempt,
        limits=_limits(),
    )

    assert result.reference == _intent(content).reference
    assert result.storage_locator == ("autocut/workspace/11/11111111111141118111111111111111")
    assert result.strategy == OBJECT_STORE_WRITE_STRATEGY
    assert result.reconciled_after_put_error is False
    assert result.etag == '"etag-1"'
    assert result.version_id == "version-1"
    assert client.maximum_read_size == 3
    assert len(client.put_calls) == 1
    request = client.put_calls[0]
    assert request["IfNoneMatch"] == "*"
    assert request["ChecksumAlgorithm"] == "SHA256"
    assert request["ContentLength"] == len(content)
    assert request["ContentType"] == "video/mp4"
    assert client.head_calls == [
        {
            "Bucket": "private-bucket",
            "Key": result.storage_locator,
            "ChecksumMode": "ENABLED",
        }
    ]


def test_acknowledgement_loss_reconciles_exact_existing_object(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    client.put_error = TimeoutError("secret endpoint")

    result = _store(client).put_path(
        intent,
        source_path=output,
        attempt_directory=attempt,
        limits=_limits(),
    )

    assert result.reference == intent.reference
    assert result.reconciled_after_put_error is True
    assert result.etag == '"existing-etag"'


def test_precondition_failure_replays_an_exact_existing_object(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)

    result = _store(client).put_path(
        intent,
        source_path=output,
        attempt_directory=attempt,
        limits=_limits(),
    )

    assert result.reference == intent.reference
    assert result.reconciled_after_put_error is True
    assert result.etag == '"existing-etag"'


def test_precondition_conflict_with_different_object_fails_closed(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    key = next(iter(client.objects))
    client.objects[key]["ChecksumSHA256"] = "different"

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(client).put_path(
            intent,
            source_path=output,
            attempt_directory=attempt,
            limits=_limits(),
        )

    assert caught.value.code == "OBJECT_STORE_REMOTE_CONFLICT"
    assert caught.value.outcome == "failed"
    assert "bucket" not in caught.value.detail
    assert "provider detail" not in caught.value.detail


@pytest.mark.parametrize("head_code", ["NoSuchKey", "RequestTimeout"])
def test_put_error_without_exact_head_is_indeterminate(
    tmp_path: Path,
    head_code: str,
) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    client = _FakeS3()
    client.put_error = TimeoutError("ambiguous put")
    client.head_error = _ProviderError(head_code)

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(client).put_path(
            _intent(content),
            source_path=output,
            attempt_directory=attempt,
            limits=_limits(),
        )

    assert caught.value.code == "OBJECT_STORE_RESULT_INDETERMINATE"
    assert caught.value.outcome == "indeterminate"


def test_remote_head_mismatch_after_successful_put_fails(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    client = _FakeS3()

    def corrupt_metadata() -> None:
        return None

    original_head = client.head_object

    def mismatched_head(**kwargs: object) -> Mapping[str, object]:
        response = dict(original_head(**kwargs))
        response["ContentLength"] = len(content) + 1
        return response

    client.after_read = corrupt_metadata
    client.head_object = mismatched_head  # type: ignore[method-assign]

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(client).put_path(
            _intent(content),
            source_path=output,
            attempt_directory=attempt,
            limits=_limits(),
        )

    assert caught.value.code == "OBJECT_STORE_REMOTE_INTEGRITY_FAILED"
    assert caught.value.outcome == "failed"


def test_source_mutation_during_upload_is_never_accepted(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    client = _FakeS3()

    def mutate() -> None:
        output.chmod(0o600)
        output.write_bytes(b"changed-render-bytes")
        output.chmod(0o400)

    client.after_read = mutate

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(client).put_path(
            _intent(content),
            source_path=output,
            attempt_directory=attempt,
            limits=_limits(),
        )

    assert caught.value.code == "OBJECT_STORE_SOURCE_INTEGRITY_FAILED"
    assert caught.value.outcome == "failed"


def test_wrong_declared_hash_is_rejected_before_external_call(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    client = _FakeS3()
    intent = PendingObjectIntent(
        UUID("11111111-1111-4111-8111-111111111111"),
        "sha256:" + "0" * 64,
        len(content),
        "video/mp4",
    )

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(client).put_path(
            _reservation_for(intent),
            source_path=output,
            attempt_directory=attempt,
            limits=_limits(),
        )

    assert caught.value.code == "OBJECT_STORE_SOURCE_INTEGRITY_FAILED"
    assert caught.value.outcome == "denied"
    assert client.put_calls == []
    assert client.head_calls == []


def test_oversize_is_denied_before_open_or_external_call(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    client = _FakeS3()

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(client).put_path(
            _intent(content),
            source_path=output,
            attempt_directory=attempt,
            limits=_limits(len(content) - 1),
        )

    assert caught.value.code == "OBJECT_STORE_SOURCE_LIMIT_EXCEEDED"
    assert caught.value.outcome == "denied"
    assert client.put_calls == []


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "writable", "outside", "fifo"])
def test_unsafe_output_identity_is_denied_before_external_call(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    client = _FakeS3()
    selected = output
    if unsafe_kind == "symlink":
        selected = attempt / "alias.mp4"
        selected.symlink_to(output)
    elif unsafe_kind == "hardlink":
        os.link(output, attempt / "second-name.mp4")
    elif unsafe_kind == "writable":
        output.chmod(0o600)
    elif unsafe_kind == "outside":
        selected = tmp_path / "outside.mp4"
        selected.write_bytes(content)
        selected.chmod(0o400)
    else:
        output.unlink()
        os.mkfifo(output, mode=0o400)

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(client).put_path(
            _intent(content),
            source_path=selected,
            attempt_directory=attempt,
            limits=_limits(),
        )

    assert caught.value.code == "OBJECT_STORE_SOURCE_UNSAFE"
    assert caught.value.outcome == "denied"
    assert client.put_calls == []


def test_unsafe_path_error_does_not_retain_or_render_local_path(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, _ = _attempt_output(tmp_path, content)
    missing = attempt / "missing.mp4"

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(_FakeS3()).put_path(
            _intent(content),
            source_path=missing,
            attempt_directory=attempt,
            limits=_limits(),
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert str(missing) not in rendered


def test_embedded_nul_path_is_closed_as_a_safe_denial(tmp_path: Path) -> None:
    content = b"rendered-mp4-bytes"
    attempt, _ = _attempt_output(tmp_path, content)
    malformed = attempt / "bad\0name.mp4"

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(_FakeS3()).put_path(
            _intent(content),
            source_path=malformed,
            attempt_directory=attempt,
            limits=_limits(),
        )

    assert caught.value.code == "OBJECT_STORE_SOURCE_UNSAFE"
    assert caught.value.outcome == "denied"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_config_and_intent_schemas_reject_unsafe_or_empty_values() -> None:
    with pytest.raises(ValueError, match="key_prefix"):
        S3ObjectStoreConfig("workspace-s3", "local-primary", "bucket", "../escape")
    with pytest.raises(ValueError, match="content_hash"):
        PendingObjectIntent(
            UUID("11111111-1111-4111-8111-111111111111"),
            "not-a-hash",
            1,
            "video/mp4",
        )
    with pytest.raises(ValueError, match="byte_length"):
        PendingObjectIntent(
            UUID("11111111-1111-4111-8111-111111111111"),
            "sha256:" + "0" * 64,
            0,
            "video/mp4",
        )
    with pytest.raises(ValueError, match="UUIDv4"):
        PendingObjectIntent(UUID(int=0), "sha256:" + "0" * 64, 1, "video/mp4")


def test_upload_requires_reservation_and_verification_is_adapter_bound(
    tmp_path: Path,
) -> None:
    content = b"rendered-mp4-bytes"
    attempt, output = _attempt_output(tmp_path, content)
    client = _FakeS3()
    raw_intent = PendingObjectIntent(
        UUID("11111111-1111-4111-8111-111111111111"),
        _sha256(content),
        len(content),
        "video/mp4",
    )

    with pytest.raises(ObjectStoreWriteError) as caught:
        _store(client).put_path(
            raw_intent,
            source_path=output,
            attempt_directory=attempt,
            limits=_limits(),
        )
    assert caught.value.code == "OBJECT_STORE_REQUEST_INVALID"
    assert client.put_calls == []

    reservation = _reservation_for(raw_intent)
    adapter = _store(client)
    verified = adapter.put_path(
        reservation,
        source_path=output,
        attempt_directory=attempt,
        limits=_limits(),
    )
    assert adapter._verify_pending_object(  # pyright: ignore[reportPrivateUsage]
        reservation,
        verified,
    )

    tampered = replace(verified, etag='"forged"')
    assert not adapter._verify_pending_object(  # pyright: ignore[reportPrivateUsage]
        reservation,
        tampered,
    )
    assert not _store(_FakeS3())._verify_pending_object(  # pyright: ignore[reportPrivateUsage]
        reservation,
        verified,
    )


def test_exact_versioned_read_materializes_into_private_descriptor(
    tmp_path: Path,
) -> None:
    content = b"exact-versioned-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    client.objects[next(iter(client.objects))]["bytes"] = content
    grant = _read_grant_for(intent)
    descriptor, path = _empty_private_destination(tmp_path)
    adapter = _store(client)
    try:
        result = adapter.materialize_to_descriptor(
            grant,
            destination_descriptor=descriptor,
            limits=_read_limits(),
        )

        assert os.pread(descriptor, len(content) + 1, 0) == content
        assert result.reference == intent.reference
        assert "storage_locator" not in {field.name for field in fields(result)}
        assert adapter._verify_materialized_read(  # pyright: ignore[reportPrivateUsage]
            grant, result
        )
        assert not adapter._verify_materialized_read(  # pyright: ignore[reportPrivateUsage]
            grant, replace(result, etag='"forged"')
        )
    finally:
        os.close(descriptor)

    assert grant.storage_locator not in repr(grant)
    assert path.read_bytes() == content
    expected_request = {
        "Bucket": "private-bucket",
        "Key": intent.target.storage_locator,
        "ChecksumMode": "ENABLED",
        "VersionId": "existing-version",
    }
    assert client.head_calls == [expected_request]
    assert client.get_calls == [
        {
            **expected_request,
            "IfMatch": '"existing-etag"',
        }
    ]
    assert client.last_body is not None
    assert client.last_body.maximum_requested <= 4
    assert client.last_body.closed


def test_unversioned_read_uses_etag_fence_without_version_parameter(
    tmp_path: Path,
) -> None:
    content = b"unversioned-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    stored = client.objects[next(iter(client.objects))]
    stored["bytes"] = content
    stored["VersionId"] = None
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        _store(client).materialize_to_descriptor(
            _read_grant_for(intent, version_id=None),
            destination_descriptor=descriptor,
            limits=_read_limits(),
        )
    finally:
        os.close(descriptor)

    assert "VersionId" not in client.head_calls[0]
    assert "VersionId" not in client.get_calls[0]
    assert client.get_calls[0]["IfMatch"] == '"existing-etag"'


@pytest.mark.parametrize("body_kind", ["short", "long", "wrong-hash", "oversized-chunk"])
def test_read_rejects_non_exact_or_unbounded_response_body(
    tmp_path: Path,
    body_kind: str,
) -> None:
    content = b"bounded-object-response"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    stored = client.objects[next(iter(client.objects))]
    stored["bytes"] = content[:-1] if body_kind == "short" else content + b"x"
    if body_kind == "wrong-hash":
        stored["bytes"] = b"x" * len(content)
    if body_kind == "oversized-chunk":
        stored["bytes"] = content
        client.get_violates_read_bound = True
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                _read_grant_for(intent),
                destination_descriptor=descriptor,
                limits=_read_limits(chunk=4),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "OBJECT_STORE_REMOTE_INTEGRITY_FAILED"
    assert caught.value.outcome == "failed"


def test_read_hashes_bytes_when_provider_checksum_is_unavailable(tmp_path: Path) -> None:
    content = b"hash-authoritative-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    stored = client.objects[next(iter(client.objects))]
    stored["bytes"] = content
    del stored["ChecksumSHA256"]
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        result = _store(client).materialize_to_descriptor(
            _read_grant_for(intent),
            destination_descriptor=descriptor,
            limits=_read_limits(),
        )
    finally:
        os.close(descriptor)

    assert result.reference.content_hash == _sha256(content)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("ContentLength", 999),
        ("ContentType", "application/octet-stream"),
        ("ChecksumSHA256", "wrong-checksum"),
        ("ETag", '"wrong-etag"'),
        ("VersionId", "wrong-version"),
        ("Metadata", {"autocut-object-id": "wrong"}),
    ],
)
def test_read_rejects_head_metadata_mismatch(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    content = b"metadata-bound-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    stored = client.objects[next(iter(client.objects))]
    stored["bytes"] = content
    stored[field] = replacement
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                _read_grant_for(intent),
                destination_descriptor=descriptor,
                limits=_read_limits(),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "OBJECT_STORE_REMOTE_INTEGRITY_FAILED"
    assert client.get_calls == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("ChecksumSHA256", "wrong-checksum"),
        ("VersionId", "wrong-version"),
        ("Metadata", {"autocut-content-sha256": "sha256:" + "0" * 64}),
    ],
)
def test_read_rejects_get_metadata_mismatch_after_exact_head(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    content = b"get-metadata-bound-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    client.objects[next(iter(client.objects))]["bytes"] = content
    client.get_overrides[field] = replacement
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                _read_grant_for(intent),
                destination_descriptor=descriptor,
                limits=_read_limits(),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "OBJECT_STORE_REMOTE_INTEGRITY_FAILED"
    assert client.last_body is not None and client.last_body.closed


@pytest.mark.parametrize("client_type", [_RaisingMetadataS3, _NonMappingBodyS3])
def test_read_closes_body_for_malformed_provider_envelope(
    tmp_path: Path,
    client_type: type[_FakeS3],
) -> None:
    content = b"malformed-provider-envelope"
    intent = _intent(content)
    client = client_type()
    _seed_exact(client, intent)
    client.objects[next(iter(client.objects))]["bytes"] = content
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                _read_grant_for(intent),
                destination_descriptor=descriptor,
                limits=_read_limits(),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "OBJECT_STORE_RESULT_INDETERMINATE"
    assert client.last_body is not None and client.last_body.closed


@pytest.mark.parametrize(
    ("grant_change", "value"),
    [
        ("backend_id", "foreign-s3"),
        ("storage_region", "foreign-region"),
        ("storage_locator", "autocut/workspace/ff/ffffffffffffffffffffffffffffffff"),
    ],
)
def test_read_rejects_grant_for_foreign_adapter_or_locator(
    tmp_path: Path,
    grant_change: str,
    value: str,
) -> None:
    content = b"adapter-bound-render"
    intent = _intent(content)
    client = _FakeS3()
    grant_arguments: dict[str, object] = {grant_change: value}
    grant = _read_grant_for(intent, **grant_arguments)  # type: ignore[arg-type]
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                grant,
                destination_descriptor=descriptor,
                limits=_read_limits(),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "OBJECT_STORE_READ_REQUEST_INVALID"
    assert client.head_calls == []
    assert client.get_calls == []


@pytest.mark.parametrize(
    ("provider_stage", "provider_code", "expected_code", "expected_outcome"),
    [
        ("head", "NoSuchKey", "OBJECT_STORE_REMOTE_INTEGRITY_FAILED", "failed"),
        ("head", "RequestTimeout", "OBJECT_STORE_RESULT_INDETERMINATE", "indeterminate"),
        ("get", "NoSuchVersion", "OBJECT_STORE_REMOTE_INTEGRITY_FAILED", "failed"),
        ("get", "RequestTimeout", "OBJECT_STORE_RESULT_INDETERMINATE", "indeterminate"),
    ],
)
def test_read_closes_provider_missing_and_unknown_results(
    tmp_path: Path,
    provider_stage: str,
    provider_code: str,
    expected_code: str,
    expected_outcome: str,
) -> None:
    content = b"provider-result-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    client.objects[next(iter(client.objects))]["bytes"] = content
    if provider_stage == "head":
        client.head_error = _ProviderError(provider_code)
    else:
        client.get_error = _ProviderError(provider_code)
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                _read_grant_for(intent),
                destination_descriptor=descriptor,
                limits=_read_limits(),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == expected_code
    assert caught.value.outcome == expected_outcome
    assert "provider detail" not in caught.value.detail


@pytest.mark.parametrize(
    "response_override",
    [
        ["not", "a", "mapping"],
        _RaisingMetadataMapping(_FakeBody(b"unused-head-body")),
    ],
)
def test_read_rejects_malformed_head_without_leaking_provider_exception(
    tmp_path: Path,
    response_override: object,
) -> None:
    content = b"malformed-head-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    client.head_response_override = response_override
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                _read_grant_for(intent),
                destination_descriptor=descriptor,
                limits=_read_limits(),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "OBJECT_STORE_RESULT_INDETERMINATE"
    assert caught.value.outcome == "indeterminate"
    assert client.get_calls == []


def test_read_closes_closeable_body_when_read_method_is_missing(
    tmp_path: Path,
) -> None:
    content = b"malformed-body-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    client.objects[next(iter(client.objects))]["bytes"] = content
    body = _CloseOnlyBody()
    client.get_body_override = body
    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                _read_grant_for(intent),
                destination_descriptor=descriptor,
                limits=_read_limits(),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "OBJECT_STORE_RESULT_INDETERMINATE"
    assert body.closed


def test_read_detects_destination_mutation_before_issuing_proof(tmp_path: Path) -> None:
    content = b"mutation-sensitive-render"
    intent = _intent(content)
    client = _FakeS3()
    _seed_exact(client, intent)
    client.objects[next(iter(client.objects))]["bytes"] = content
    descriptor, _ = _empty_private_destination(tmp_path)
    client.get_after_exhausted = lambda: os.pwrite(descriptor, b"X", 0)
    try:
        with pytest.raises(ObjectStoreReadError) as caught:
            _store(client).materialize_to_descriptor(
                _read_grant_for(intent),
                destination_descriptor=descriptor,
                limits=_read_limits(),
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "OBJECT_STORE_READ_TARGET_UNSAFE"
    assert caught.value.outcome == "failed"


def test_read_rejects_forged_grant_unsafe_descriptor_and_limit(
    tmp_path: Path,
) -> None:
    content = b"closed-read-request"
    intent = _intent(content)
    with pytest.raises(ValueError, match="not issued"):
        _S3ReadGrant(  # pyright: ignore[reportPrivateUsage]
            reference=intent.reference,
            backend_id="workspace-s3",
            storage_region="local-primary",
            storage_locator=intent.target.storage_locator,
            etag='"existing-etag"',
            version_id="existing-version",
            write_strategy=OBJECT_STORE_WRITE_STRATEGY,
            _attestation=object(),
        )

    client = _FakeS3()
    grant = _read_grant_for(intent)
    path = tmp_path / "write-only"
    write_only = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with pytest.raises(ObjectStoreReadError) as unsafe:
            _store(client).materialize_to_descriptor(
                grant,
                destination_descriptor=write_only,
                limits=_read_limits(),
            )
    finally:
        os.close(write_only)
    assert unsafe.value.code == "OBJECT_STORE_READ_TARGET_UNSAFE"

    descriptor, _ = _empty_private_destination(tmp_path)
    try:
        with pytest.raises(ObjectStoreReadError) as oversized:
            _store(client).materialize_to_descriptor(
                grant,
                destination_descriptor=descriptor,
                limits=_read_limits(maximum=len(content) - 1),
            )
    finally:
        os.close(descriptor)
    assert oversized.value.code == "OBJECT_STORE_READ_LIMIT_EXCEEDED"
    assert client.head_calls == []
