from __future__ import annotations

import base64
import hashlib
import os
import traceback
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

import pytest
from autocut_kernel.store.object_store import (
    OBJECT_STORE_WRITE_STRATEGY,
    ObjectStoreWriteError,
    ObjectStoreWriteLimits,
    PendingObjectIntent,
    S3ObjectStoreConfig,
    S3PendingObjectStore,
    _issue_pending_object_reservation,  # pyright: ignore[reportPrivateUsage]
    _PendingObjectReservation,  # pyright: ignore[reportPrivateUsage]
    _PendingObjectTarget,  # pyright: ignore[reportPrivateUsage]
)


class _ProviderError(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}
        super().__init__("provider detail that must never escape")


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.put_calls: list[dict[str, object]] = []
        self.head_calls: list[dict[str, object]] = []
        self.put_error: Exception | None = None
        self.head_error: Exception | None = None
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
        key = (cast(str, kwargs["Bucket"]), cast(str, kwargs["Key"]))
        try:
            stored = self.objects[key]
        except KeyError as error:
            raise _ProviderError("NoSuchKey") from error
        return {name: value for name, value in stored.items() if name != "bytes"}


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
