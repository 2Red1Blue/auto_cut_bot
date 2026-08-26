"""Strict bounded decoders for the isolated physical root contract.

These functions restore a ``PhysicalRootMediaEvidence`` value, not commitment,
ownership or Admission. Every field is required; no defaults, normalization or
reflection are used. The bytes entry point requires a caller-owned explicit
size bound and reuses the shared bounded strict-JSON parser.
"""

from __future__ import annotations

from typing import cast

from .physical_root import PhysicalRootMediaEvidence
from .root_evidence_codec import (
    decode_audio_sample_boundary_set,
    decode_frame_pts_index_set,
    decode_media_evidence_json,
    decode_scene_boundary_set,
    decode_shot_boundary_set,
    decode_subtitle_cue_set,
    decode_visual_validity_set,
)
from .types import MediaValidationError, sha256_prefixed


def _object(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise MediaValidationError("physical root must be a JSON object")
    item = cast(dict[object, object], value)
    if any(type(key) is not str for key in item) or set(item) != set(fields):  # noqa: E721
        raise MediaValidationError("physical root object has missing or unknown fields")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise MediaValidationError("physical root text must be an exact nonempty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise MediaValidationError("physical root text must be valid UTF-8") from error
    return value


def _hash(value: object) -> str:
    return sha256_prefixed(_text(value), "physical root hash")


def decode_physical_root_media_evidence(value: object) -> PhysicalRootMediaEvidence:
    """Decode one closed physical-root object into an immutable value."""
    item = _object(
        value,
        (
            "physical_root_id",
            "source_id",
            "source_sha256",
            "source_manifest_sha256",
            "root_input_manifest_sha256",
            "frame_pts_index",
            "shot_boundaries",
            "scene_boundaries",
            "audio_sample_boundaries",
            "visual_validity",
            "subtitle_cues",
        ),
    )
    return PhysicalRootMediaEvidence(
        _text(item["physical_root_id"]),
        _text(item["source_id"]),
        _hash(item["source_sha256"]),
        _hash(item["source_manifest_sha256"]),
        _hash(item["root_input_manifest_sha256"]),
        decode_frame_pts_index_set(item["frame_pts_index"]),
        decode_shot_boundary_set(item["shot_boundaries"]),
        decode_scene_boundary_set(item["scene_boundaries"]),
        decode_audio_sample_boundary_set(item["audio_sample_boundaries"]),
        decode_visual_validity_set(item["visual_validity"]),
        decode_subtitle_cue_set(item["subtitle_cues"]),
    )


def decode_physical_root_media_evidence_json(
    raw: bytes, *, max_bytes: int,
) -> PhysicalRootMediaEvidence:
    """Decode bounded strict UTF-8 JSON; formatting does not confer authority."""
    return decode_physical_root_media_evidence(decode_media_evidence_json(raw, max_bytes=max_bytes))
