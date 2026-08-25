from __future__ import annotations

from dataclasses import FrozenInstanceError

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


def _policy() -> VlmParsePolicy:
    return VlmParsePolicy(
        max_response_bytes=32_000,
        max_entities=8,
        max_facts=16,
        max_events=16,
        max_candidate_hypotheses=8,
        max_temporal_segments=8,
        max_measurements=16,
        max_text_characters=512,
        max_total_text_characters=8_192,
    )


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
        core_range=TickRange(1_000, 1_100),
        frame_pts_index_set=frame_pts_set(
            source_id="source-001",
            source_sha256=_hash("a"),
            clock_id="video-clock-0",
            time_base=time_base,
            origin_tick=1_000,
            end_tick=1_100,
            ticks=(1_010, 1_050, 1_090),
        ),
        proxy_blob_ref=WindowProxyBlobRef("proxy-001", _hash("b"), 4_096, "video/mp4"),
        preprocess_policy_sha256=_hash("c"),
        window_sampling_policy_sha256=_hash("4"),
        timeline_map=timeline,
        frame_samples=(
            WindowFrameSample(1_010, 10, _hash("d")),
            WindowFrameSample(1_050, 50, _hash("e")),
            WindowFrameSample(1_090, 90, _hash("f")),
        ),
    )


def test_request_identity_binds_manifest_request_and_structural_policy() -> None:
    manifest = _manifest()
    manifest_set = WindowManifestSet(
        manifest.source_id,
        manifest.source_clock_id,
        manifest.source_sha256,
        manifest.stream_index,
        manifest.source_time_base,
        manifest.core_range,
        (manifest,),
    )
    identity = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=_hash("1"),
        prompt_version="semantic-pack-v3",
        response_schema_sha256=_hash("2"),
        model_id="vlm-v3",
        provider_id="provider",
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        parse_policy=_policy(),
    )

    assert identity.parse_policy_sha256 == _policy().canonical_hash
    assert "minimum_confidence" not in _policy().to_mapping()
    with pytest.raises(FrozenInstanceError):
        identity.model_id = "forged"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("max_response_bytes", True), ("max_facts", 0), ("max_events", 1.5)],
)
def test_parse_policy_accepts_only_positive_integer_resource_budgets(
    field_name: str, value: object
) -> None:
    values: dict[str, object] = dict(_policy().to_mapping())
    values[field_name] = value
    with pytest.raises((VlmValidationError, ValueError)):
        VlmParsePolicy(**values)  # type: ignore[arg-type]


def test_parse_policy_rejects_per_field_budget_larger_than_total() -> None:
    values = dict(_policy().to_mapping())
    values["max_text_characters"] = 10_000
    with pytest.raises(VlmValidationError, match="per-field"):
        VlmParsePolicy(**values)
