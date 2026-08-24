from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    VlmParsePolicy,
    VlmRequestIdentity,
    VlmValidationError,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)

from . import frame_pts_set


def _hash(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _manifest() -> WindowManifest:
    time_base = TimeBase(1, 1_000)
    timeline = ProxyTimelineMap.translation(
        time_base=time_base,
        proxy_range=TickRange(0, 100),
        source_start_pts=1_000,
        max_source_error_pts=1,
    )
    return WindowManifest(
        source_id="source-001",
        source_clock_id="video-clock-0",
        source_sha256=_hash("a"),
        stream_index=0,
        source_time_base=time_base,
        source_range=TickRange(1_000, 1_100),
        core_range=TickRange(1_020, 1_080),
        frame_pts_index_set=frame_pts_set(
            source_id="source-001",
            source_sha256=_hash("a"),
            clock_id="video-clock-0",
            time_base=time_base,
            origin_tick=1_000,
            end_tick=1_100,
            ticks=(1_000, 1_010, 1_073, 1_099),
        ),
        proxy_blob_ref=WindowProxyBlobRef("proxy-001", _hash("b"), 4_096, "video/mp4"),
        preprocess_policy_sha256=_hash("c"),
        window_sampling_policy_sha256=_hash("4"),
        timeline_map=timeline,
        frame_samples=(
            WindowFrameSample(source_pts=1_010, proxy_pts=10, frame_sha256=_hash("d")),
            WindowFrameSample(source_pts=1_073, proxy_pts=73, frame_sha256=_hash("e")),
        ),
    )


def _policy() -> VlmParsePolicy:
    return VlmParsePolicy(Decimal("0.80"), 8_192, 8, 256, 1_024)


def _manifest_set(manifest: WindowManifest) -> WindowManifestSet:
    return WindowManifestSet(
        source_id=manifest.source_id,
        source_clock_id=manifest.source_clock_id,
        source_sha256=manifest.source_sha256,
        stream_index=manifest.stream_index,
        source_time_base=manifest.source_time_base,
        declared_source_range=manifest.core_range,
        manifests=(manifest,),
    )


def test_request_identity_canonically_binds_window_prompt_model_parameters_and_policy() -> None:
    manifest = _manifest()
    manifest_set = _manifest_set(manifest)
    first = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=_hash("1"),
        prompt_version="story-evidence-v1",
        response_schema_sha256=_hash("2"),
        model_id="vlm-model-v1",
        provider_id="fake-provider",
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        parse_policy=_policy(),
    )
    second = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=_hash("1"),
        prompt_version="story-evidence-v1",
        response_schema_sha256=_hash("2"),
        model_id="vlm-model-v1",
        provider_id="fake-provider",
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        parse_policy=_policy(),
    )

    assert first.canonical_hash == second.canonical_hash
    assert first.window_manifest_sha256 == manifest.canonical_hash
    assert first.frame_samples_sha256 == manifest.frame_samples_sha256
    assert first.source_sha256 == manifest.source_sha256
    with pytest.raises(FrozenInstanceError):
        first.model_id = "changed"  # type: ignore[misc]


def test_request_identity_changes_when_a_sample_hash_or_request_parameter_changes() -> None:
    original = _manifest()
    changed_frame = WindowManifest(
        source_id=original.source_id,
        source_clock_id=original.source_clock_id,
        source_sha256=original.source_sha256,
        stream_index=original.stream_index,
        source_time_base=original.source_time_base,
        source_range=original.source_range,
        core_range=original.core_range,
        frame_pts_index_set=original.frame_pts_index_set,
        proxy_blob_ref=original.proxy_blob_ref,
        preprocess_policy_sha256=original.preprocess_policy_sha256,
        window_sampling_policy_sha256=original.window_sampling_policy_sha256,
        timeline_map=original.timeline_map,
        frame_samples=(
            WindowFrameSample(1_010, 10, _hash("f")),
            original.frame_samples[1],
        ),
    )
    common = {
        "prompt_template_sha256": _hash("1"),
        "prompt_version": "story-evidence-v1",
        "response_schema_sha256": _hash("2"),
        "model_id": "vlm-model-v1",
        "provider_id": "fake-provider",
        "parse_policy": _policy(),
    }
    first = VlmRequestIdentity.from_manifest(
        original,
        _manifest_set(original),
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        **common,
    )
    second = VlmRequestIdentity.from_manifest(
        changed_frame,
        _manifest_set(changed_frame),
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        **common,
    )
    third = VlmRequestIdentity.from_manifest(
        original,
        _manifest_set(original),
        request_parameters_sha256=_hash("5"),
        request_payload_sha256=_hash("6"),
        **common,
    )

    assert len({first.canonical_hash, second.canonical_hash, third.canonical_hash}) == 3


def test_proxy_blob_ref_is_immutable_and_has_no_locator_surface() -> None:
    proxy = WindowProxyBlobRef("proxy-001", _hash("b"), 4_096, "video/mp4")

    assert proxy.to_mapping() == {
        "byte_length": 4_096,
        "content_hash": _hash("b"),
        "media_type": "video/mp4",
        "object_id": "proxy-001",
    }
    assert "locator" not in proxy.to_mapping()
    with pytest.raises(TypeError):
        WindowProxyBlobRef(  # type: ignore[call-arg]
            object_id="proxy-001",
            content_hash=_hash("b"),
            byte_length=4_096,
            media_type="video/mp4",
            storage_locator="file:///tmp/proxy.mp4",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_confidence", 0.8),
        ("max_response_bytes", True),
        ("max_observations", 0),
    ],
)
def test_parse_policy_rejects_float_bool_and_non_positive_limits(field: str, value: object) -> None:
    values: dict[str, object] = {
        "minimum_confidence": Decimal("0.8"),
        "max_response_bytes": 1_000,
        "max_observations": 4,
        "max_summary_characters": 100,
        "max_total_summary_characters": 200,
    }
    values[field] = value
    with pytest.raises((VlmValidationError, ValueError)):
        VlmParsePolicy(**values)  # type: ignore[arg-type]
