"""Network-free regression tests for Ark file cache and response formats."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from autocut_core.semantic.engine import ark_responses


def _cache_file(tmp_path: Path, *, uploaded_at: object, file_id: str = "file-cached") -> Path:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    cache_file = tmp_path / "cache" / "ark_files" / ("a" * 24 + ".json")
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps({"file_id": file_id, "uploaded_at": uploaded_at}))
    return media


@pytest.mark.parametrize(
    "uploaded_at",
    [
        None,
        "not-a-date",
        (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=6)).isoformat(),
    ],
)
def test_stale_or_ambiguous_cache_is_reuploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uploaded_at: object
) -> None:
    media = _cache_file(tmp_path, uploaded_at=uploaded_at)
    uploads: list[str] = []
    monkeypatch.setattr(
        ark_responses, "_upload_file_to_ark", lambda **_: uploads.append("new") or "file-new"
    )
    monkeypatch.setattr(ark_responses, "_check_file_active", lambda *_args, **_kwargs: True)

    assert (
        ark_responses._get_or_upload_file_id(
            client=object(),
            file_path=media,
            file_sha256="a" * 64,
            extra_headers=None,
            video_fps=1.0,
            cache_dir=tmp_path / "cache",
        )
        == "file-new"
    )
    assert uploads == ["new"]


def test_fresh_aware_cache_is_reused_only_when_remote_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _cache_file(
        tmp_path,
        uploaded_at=(
            datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            - datetime.timedelta(hours=1)
        ).isoformat(),
    )
    monkeypatch.setattr(
        ark_responses, "_upload_file_to_ark", lambda **_: pytest.fail("unexpected upload")
    )
    monkeypatch.setattr(ark_responses, "_check_file_active", lambda *_args, **_kwargs: True)

    assert (
        ark_responses._get_or_upload_file_id(
            client=object(),
            file_path=media,
            file_sha256="a" * 64,
            extra_headers=None,
            video_fps=1.0,
            cache_dir=tmp_path / "cache",
        )
        == "file-cached"
    )


def test_fresh_naive_cache_is_interpreted_as_utc_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _cache_file(
        tmp_path,
        uploaded_at=(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1))
        .replace(tzinfo=None)
        .isoformat(),
    )
    monkeypatch.setattr(
        ark_responses, "_upload_file_to_ark", lambda **_: pytest.fail("unexpected upload")
    )
    monkeypatch.setattr(ark_responses, "_check_file_active", lambda *_args, **_kwargs: True)

    assert (
        ark_responses._get_or_upload_file_id(
            client=object(),
            file_path=media,
            file_sha256="a" * 64,
            extra_headers=None,
            video_fps=1.0,
            cache_dir=tmp_path / "cache",
        )
        == "file-cached"
    )


def test_remote_invalid_cache_is_reuploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _cache_file(
        tmp_path, uploaded_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    monkeypatch.setattr(ark_responses, "_check_file_active", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(ark_responses, "_upload_file_to_ark", lambda **_: "file-new")

    assert (
        ark_responses._get_or_upload_file_id(
            client=object(),
            file_path=media,
            file_sha256="a" * 64,
            extra_headers=None,
            video_fps=1.0,
            cache_dir=tmp_path / "cache",
        )
        == "file-new"
    )


def test_response_format_translation_preserves_explicit_schema_contract() -> None:
    descriptor = {"name": "story_result", "strict": False, "schema": {"type": "object"}}
    assert ark_responses._build_text_format({"type": "json_schema", "json_schema": descriptor}) == {
        "format": {"type": "json_schema", **descriptor}
    }
    assert ark_responses._build_text_format({"type": "json_object", "json_schema": descriptor}) == {
        "format": {"type": "json_schema", **descriptor}
    }
    assert ark_responses._build_text_format({"type": "json_object"}) == {
        "format": {"type": "json_object"}
    }
    assert (
        ark_responses._build_text_format({"type": "json_schema", "json_schema": {"schema": {}}})
        is None
    )
    assert ark_responses._build_text_format({"type": "json_object", "json_schema": "bad"}) is None
    assert ark_responses._build_text_format({"type": "other"}) is None
