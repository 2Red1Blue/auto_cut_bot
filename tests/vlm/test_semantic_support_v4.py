from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from fractions import Fraction
from typing import cast

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    VlmSemanticSupport,
    VlmValidationError,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)
from autocut_kernel.vlm.parser_contract import vlm_parser_contract_sha256
from autocut_kernel.vlm.semantic_support_v4 import (
    FrameAnchoredObservationSupportV4,
    VideoObservationSupportV4,
    decode_support_v4,
    frame_aliases,
    parse_support_v4,
)

from . import frame_pts_set


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _context(
    *,
    time_base: TimeBase = TimeBase(1, 1_000),
    proxy_start: int = 700,
    source_start: int = 10_000,
    duration: int = 100,
    frame_offsets: tuple[int, ...] = (0, 10, 50, 99),
) -> tuple[WindowManifest, WindowManifestSet]:
    source_range = TickRange(source_start, source_start + duration)
    manifest = WindowManifest(
        source_id="source-001",
        source_clock_id="video-clock-0",
        source_sha256=_hash("a"),
        stream_index=0,
        source_time_base=time_base,
        source_range=source_range,
        core_range=source_range,
        frame_pts_index_set=frame_pts_set(
            source_id="source-001",
            source_sha256=_hash("a"),
            clock_id="video-clock-0",
            time_base=time_base,
            origin_tick=source_start,
            end_tick=source_range.end_pts,
            ticks=tuple(source_start + offset for offset in frame_offsets),
        ),
        proxy_blob_ref=WindowProxyBlobRef("proxy-object", _hash("b"), 4096, "video/mp4"),
        preprocess_policy_sha256=_hash("c"),
        window_sampling_policy_sha256=_hash("d"),
        timeline_map=ProxyTimelineMap.translation(
            time_base=time_base,
            proxy_range=TickRange(proxy_start, proxy_start + duration),
            source_start_pts=source_start,
        ),
        frame_samples=tuple(
            WindowFrameSample(source_start + offset, proxy_start + offset, _hash("e"))
            for offset in frame_offsets
        ),
    )
    return manifest, WindowManifestSet(
        source_id=manifest.source_id,
        source_clock_id=manifest.source_clock_id,
        source_sha256=manifest.source_sha256,
        stream_index=manifest.stream_index,
        source_time_base=time_base,
        declared_source_range=source_range,
        manifests=(manifest,),
    )


def _wire(start: int = 20, end: int = 30, uncertainty: int = 0) -> dict[str, object]:
    return {
        "support_kind": "video_observation",
        "interval_ms": {"start_ms": start, "end_ms": end, "uncertainty_ms": uncertainty},
        "confidence": "0.90",
    }


def _frame_wire(
    start: int = 10, end: int = 20, uncertainty: int = 0,
    refs: tuple[str, ...] = ("f0002",),
) -> dict[str, object]:
    return {
        **_wire(start, end, uncertainty),
        "support_kind": "frame_anchored_observation",
        "frame_refs": list(refs),
    }


def test_nonzero_playback_origin_is_not_source_zero() -> None:
    manifest, manifest_set = _context()
    support = parse_support_v4(_wire(), manifest, manifest_set)

    assert type(support) is VideoObservationSupportV4
    assert support.proxy_interval.proxy_range == TickRange(720, 730)
    assert support.source_interval.coarse_range == TickRange(10_020, 10_030)
    assert support.derived.exact_start_proxy_pts == Fraction(720)
    assert support.core_owner_window_manifest_sha256 == manifest.canonical_hash
    assert support.to_wire_mapping() == _wire()


def test_fractional_time_base_rounds_once_after_exact_uncertainty_plus_error() -> None:
    manifest, manifest_set = _context(time_base=TimeBase(1001, 30_000))
    support = parse_support_v4(_wire(34, 66, 1), manifest, manifest_set)

    assert support.proxy_interval.proxy_range == TickRange(701, 702)
    assert support.derived.exact_start_proxy_pts == 700 + Fraction(1020, 1001)
    assert support.derived.exact_end_proxy_pts == 700 + Fraction(1980, 1001)
    assert support.derived.start_quantization_error_proxy_pts == Fraction(19, 1001)
    assert support.derived.end_quantization_error_proxy_pts == Fraction(22, 1001)
    assert support.derived.exact_uncertainty_proxy_pts == Fraction(30, 1001)
    assert support.derived.declared_uncertainty_proxy_pts == 1
    # ceil(30/1001 + 22/1001) is one, not ceil(30/1001) + ceil(22/1001).
    assert support.proxy_interval.uncertainty_pts == 1
    assert support.source_interval.provider_uncertainty_proxy_pts == 1
    assert support.source_interval.coarse_range == TickRange(10_000, 10_003)
    assert support.to_wire_mapping() == _wire(34, 66, 1)


def test_actual_quantization_error_increases_effective_uncertainty_when_required() -> None:
    manifest, manifest_set = _context(time_base=TimeBase(1001, 30_000))
    support = parse_support_v4(_wire(1, 2, 3), manifest, manifest_set)

    assert support.derived.exact_uncertainty_proxy_pts == Fraction(90, 1001)
    assert support.derived.declared_uncertainty_proxy_pts == 1
    assert support.derived.end_quantization_error_proxy_pts == Fraction(941, 1001)
    assert support.proxy_interval.uncertainty_pts == 2  # ceil((90 + 941) / 1001)
    assert support.proxy_interval.proxy_range == TickRange(700, 701)
    assert support.source_interval.coarse_range == TickRange(10_000, 10_003)


def test_representable_duration_floors_true_duration_and_records_unusable_tail() -> None:
    manifest, manifest_set = _context(time_base=TimeBase(1, 30_000))
    support = parse_support_v4(_wire(2, 3), manifest, manifest_set)

    assert support.derived.playback_duration_ms == Fraction(10, 3)
    assert support.derived.representable_duration_ms == 3
    assert support.proxy_interval.proxy_range == TickRange(760, 790)
    assert support.derived.to_mapping()["unrepresentable_tail_ms"] == {
        "numerator": 1, "denominator": 3,
    }
    with pytest.raises(VlmValidationError, match="exceeds representable playback duration"):
        parse_support_v4(_wire(2, 4), manifest, manifest_set)


def test_last_representable_end_may_round_up_to_proxy_end_without_clamping() -> None:
    manifest, manifest_set = _context(
        time_base=TimeBase(1001, 30_000), duration=3, frame_offsets=(0, 1, 2),
    )
    support = parse_support_v4(_frame_wire(66, 100, refs=("f0003",)), manifest, manifest_set)

    assert support.derived.playback_duration_ms == Fraction(1001, 10)
    assert support.proxy_interval.proxy_range == TickRange(701, 703)
    assert support.derived.exact_end_proxy_pts == 700 + Fraction(3000, 1001)
    assert support.derived.end_quantization_error_proxy_pts == Fraction(3, 1001)
    assert support.source_interval.coarse_range.end_pts == manifest.source_range.end_pts


def test_sub_millisecond_window_is_not_expanded_to_fit_wire() -> None:
    manifest, manifest_set = _context(
        time_base=TimeBase(1, 30_000), duration=20, frame_offsets=(0, 19),
    )
    with pytest.raises(VlmValidationError, match="sub-millisecond windows"):
        parse_support_v4(_wire(0, 1), manifest, manifest_set)


@pytest.mark.parametrize(("start", "end"), [(0, 34_000), (34_000, 33_000)])
def test_invalid_response_patterns_are_rejected_not_rescaled_or_reordered(
    start: int, end: int,
) -> None:
    # Minimal representatives of the observed out-of-window and inverted supports.
    manifest, manifest_set = _context(duration=33_000)
    wire = _wire(start, end)
    original = copy.deepcopy(wire)
    with pytest.raises(VlmValidationError, match="duration|half-open"):
        parse_support_v4(wire, manifest, manifest_set)
    assert wire == original


@pytest.mark.parametrize("field_name", ["start_ms", "end_ms", "uncertainty_ms"])
@pytest.mark.parametrize("value", [-1, True, 1.0, "1", None])
def test_milliseconds_require_exact_nonnegative_integers(field_name: str, value: object) -> None:
    manifest, manifest_set = _context()
    wire = _wire()
    cast(dict[str, object], wire["interval_ms"])[field_name] = value
    with pytest.raises(VlmValidationError, match=f"{field_name} must be a non-negative integer"):
        parse_support_v4(wire, manifest, manifest_set)


def test_empty_interval_and_noncanonical_confidence_are_rejected() -> None:
    manifest, manifest_set = _context()
    with pytest.raises(VlmValidationError, match="half-open"):
        parse_support_v4(_wire(10, 10), manifest, manifest_set)
    for value in (0.9, True, "NaN", "Infinity", "1.01", "-0", "9e-1", "０.９"):
        with pytest.raises(VlmValidationError, match="canonical decimal string"):
            parse_support_v4({**_wire(), "confidence": value}, manifest, manifest_set)


def test_alias_table_is_stable_reversible_bound_and_immutable() -> None:
    manifest, _ = _context(time_base=TimeBase(1001, 30_000))
    table = frame_aliases(manifest)

    assert [entry.alias for entry in table.entries] == ["f0001", "f0002", "f0003", "f0004"]
    for entry, sample in zip(table.entries, manifest.frame_samples, strict=True):
        assert entry.frame_id == sample.frame_id
        assert entry.frame_sha256 == sample.frame_sha256
        assert entry.proxy_pts == sample.proxy_pts
        assert entry.source_pts == sample.source_pts
        assert table.by_alias[entry.alias] == entry
    assert table.entries[1].relative_time_ms == Fraction(1001, 3)
    assert frame_aliases(manifest).canonical_hash == table.canonical_hash
    changed = replace(manifest, proxy_blob_ref=replace(manifest.proxy_blob_ref, object_id="other"))
    assert frame_aliases(changed).canonical_hash != table.canonical_hash
    with pytest.raises(TypeError):
        table.by_alias["f0001"] = table.entries[1]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        table.entries[0].proxy_pts = 0  # type: ignore[misc]


def test_video_without_sampled_anchor_is_valid_but_cannot_masquerade_as_v3_support() -> None:
    manifest, manifest_set = _context()
    support = parse_support_v4(_wire(), manifest, manifest_set)

    assert not isinstance(support, VlmSemanticSupport)
    assert not hasattr(support, "frame_refs")
    assert not hasattr(support, "supporting_frame_ids")
    assert "frame_anchors" not in support.to_mapping()
    assert support.confidence == Decimal("0.90")
    with pytest.raises(FrozenInstanceError):
        support.confidence = Decimal("1")  # type: ignore[misc]
    with pytest.raises(VlmValidationError, match="frame inside the declared interval_ms"):
        parse_support_v4(_frame_wire(20, 30, uncertainty=50), manifest, manifest_set)


def test_frame_anchor_uses_exact_half_open_interval_not_outward_rounding() -> None:
    manifest, manifest_set = _context()
    at_start = parse_support_v4(_frame_wire(10, 20), manifest, manifest_set)
    assert type(at_start) is FrameAnchoredObservationSupportV4
    assert at_start.frame_anchors[0].frame_id == manifest.frame_samples[1].frame_id
    with pytest.raises(VlmValidationError, match="frame inside the declared interval_ms"):
        parse_support_v4(_frame_wire(0, 10), manifest, manifest_set)

    fractional, fractional_set = _context(
        time_base=TimeBase(1001, 30_000), frame_offsets=(0, 1, 2),
    )
    # f0002 is at 33.366...ms: rounded start tick contains it, declared 34ms does not.
    with pytest.raises(VlmValidationError, match="frame inside the declared interval_ms"):
        parse_support_v4(_frame_wire(34, 66, uncertainty=100), fractional, fractional_set)


def test_frame_branch_requires_at_least_one_inside_and_preserves_all_declared_refs() -> None:
    manifest, manifest_set = _context()
    wire = _frame_wire(refs=("f0004", "f0002"))
    support = parse_support_v4(wire, manifest, manifest_set)

    assert type(support) is FrameAnchoredObservationSupportV4
    assert support.frame_refs == ("f0004", "f0002")
    assert [anchor.proxy_pts for anchor in support.frame_anchors] == [799, 710]
    assert support.to_wire_mapping() == wire


@pytest.mark.parametrize("refs", [[], ["f0002", "f0002"], ["f1"], ["f0000"], ["f9999"], [True], None])
def test_frame_branch_rejects_empty_duplicate_unknown_or_malformed_aliases(refs: object) -> None:
    manifest, manifest_set = _context()
    with pytest.raises(VlmValidationError, match="frame_refs"):
        parse_support_v4({**_frame_wire(), "frame_refs": refs}, manifest, manifest_set)


def test_union_and_nested_interval_have_exact_closed_fields() -> None:
    manifest, manifest_set = _context()
    for field_name in ("frame_refs", "supporting_frame_ids", "approved", "source_range"):
        with pytest.raises(VlmValidationError, match="support must contain exactly"):
            parse_support_v4({**_wire(), field_name: []}, manifest, manifest_set)
    with pytest.raises(VlmValidationError, match="support must contain exactly"):
        parse_support_v4({**_wire(), "support_kind": "frame_anchored_observation"}, manifest, manifest_set)
    with pytest.raises(VlmValidationError, match="support_kind is not registered"):
        parse_support_v4({**_wire(), "support_kind": "confirmed_fact"}, manifest, manifest_set)
    for interval in ({"start_ms": 1, "end_ms": 2}, {"start_ms": 1, "end_ms": 2, "uncertainty_ms": 0, "unit": "ms"}):
        with pytest.raises(VlmValidationError, match="interval_ms must contain exactly"):
            parse_support_v4({**_wire(), "interval_ms": interval}, manifest, manifest_set)


def test_context_membership_and_core_owner_come_from_registered_manifest_set() -> None:
    manifest, manifest_set = _context(duration=200)
    changed = replace(manifest, proxy_blob_ref=replace(manifest.proxy_blob_ref, object_id="other"))
    with pytest.raises(VlmValidationError, match="exact WindowManifestSet member"):
        parse_support_v4(_wire(), changed, manifest_set)

    left = replace(manifest, core_range=TickRange(10_000, 10_100))
    right = replace(manifest, core_range=TickRange(10_100, 10_200))
    shared_set = replace(manifest_set, manifests=(left, right))
    support = parse_support_v4(_wire(110, 120), left, shared_set)
    assert support.core_owner_window_manifest_sha256 == right.canonical_hash
    assert support.source_interval.coarse_range == TickRange(10_110, 10_120)


@pytest.mark.parametrize("anchored", [False, True])
def test_persisted_round_trip_rederives_identity_without_mutating_wire(anchored: bool) -> None:
    manifest, manifest_set = _context()
    wire = _frame_wire() if anchored else _wire()
    original = copy.deepcopy(wire)
    support = parse_support_v4(wire, manifest, manifest_set)
    stored: object = json.loads(json.dumps(support.to_mapping()))

    decoded = decode_support_v4(stored, manifest, manifest_set)
    assert decoded == support
    assert decoded.to_wire_mapping() == original
    assert wire == original


@pytest.mark.parametrize("tamper", ["blob", "manifest", "origin", "false_zero", "frame", "new_key"])
def test_persisted_decoder_rejects_forged_binding_conversion_or_frame(tamper: str) -> None:
    manifest, manifest_set = _context()
    stored = parse_support_v4(_frame_wire(), manifest, manifest_set).to_mapping()
    binding = cast(dict[str, object], stored["binding"])
    derived = cast(dict[str, object], stored["derived"])
    if tamper == "blob":
        cast(dict[str, object], binding["proxy_blob_ref"])["content_hash"] = _hash("f")
    elif tamper == "manifest":
        binding["window_manifest_sha256"] = _hash("f")
    elif tamper == "origin":
        derived["proxy_origin_pts"] = 0
    elif tamper == "false_zero":
        derived["declared_uncertainty_proxy_pts"] = False
    elif tamper == "frame":
        cast(list[dict[str, object]], stored["frame_anchors"])[0]["frame_id"] = _hash("f")
    else:
        stored["approved"] = True
    with pytest.raises(VlmValidationError, match="differs from|support must contain exactly"):
        decode_support_v4(stored, manifest, manifest_set)


def test_v3_parser_implementation_bundle_hash_is_byte_exact_unchanged() -> None:
    assert vlm_parser_contract_sha256() == (
        "sha256:6963125a7ac28e2131b0473dd9de818b97ad8cc7f003359cf73d1b877b7f0a19"
    )
