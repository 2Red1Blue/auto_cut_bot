from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    VlmParsePolicy,
    VlmRequestIdentity,
    VlmResponseIndeterminate,
    VlmResponseRejected,
    VlmValidationError,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
    parse_vlm_response,
    select_core_owner,
)

from . import frame_pts_set


def _hash(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _context() -> tuple[
    WindowManifest,
    WindowManifestSet,
    VlmParsePolicy,
    VlmRequestIdentity,
]:
    time_base = TimeBase(1, 1_000)
    timeline = ProxyTimelineMap.translation(
        time_base=time_base,
        proxy_range=TickRange(0, 100),
        source_start_pts=1_000,
        max_source_error_pts=1,
    )
    manifest = WindowManifest(
        source_id="source-001",
        source_clock_id="video-clock-0",
        source_sha256=_hash("a"),
        stream_index=0,
        source_time_base=time_base,
        source_range=TickRange(1_000, 1_100),
        core_range=TickRange(1_010, 1_090),
        frame_pts_index_set=frame_pts_set(
            source_id="source-001",
            source_sha256=_hash("a"),
            clock_id="video-clock-0",
            time_base=time_base,
            origin_tick=1_000,
            end_tick=1_100,
            ticks=(1_000, 1_010, 1_050, 1_090, 1_099),
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
    owner = replace(
        manifest,
        core_range=TickRange(1_000, 1_010),
        proxy_blob_ref=WindowProxyBlobRef("proxy-owner", _hash("8"), 4_096, "video/mp4"),
    )
    manifest_set = WindowManifestSet(
        source_id=manifest.source_id,
        source_clock_id=manifest.source_clock_id,
        source_sha256=manifest.source_sha256,
        stream_index=manifest.stream_index,
        source_time_base=manifest.source_time_base,
        declared_source_range=TickRange(1_000, 1_090),
        manifests=(owner, manifest),
    )
    policy = VlmParsePolicy(Decimal("0.80"), 4_096, 4, 128, 256)
    identity = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=_hash("1"),
        prompt_version="story-evidence-v1",
        response_schema_sha256=_hash("2"),
        model_id="vlm-model-v1",
        provider_id="fake-provider",
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        parse_policy=policy,
    )
    return manifest, manifest_set, policy, identity


def _observation(manifest: WindowManifest, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "confidence": "0.91",
        "kind": "change",
        "proxy_interval": {"end_pts": 60, "start_pts": 40, "uncertainty_pts": 2},
        "summary": "A registered visible state changes.",
        "supporting_frame_ids": [manifest.frame_samples[1].frame_id],
    }
    value.update(changes)
    return value


def _raw(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def test_strict_parser_derives_provenance_and_coarse_interval_from_request() -> None:
    manifest, manifest_set, policy, identity = _context()
    raw = _raw({"schema_version": 1, "observations": [_observation(manifest)]})

    result = parse_vlm_response(
        raw,
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=policy,
    )

    observation = result.observations[0]
    assert observation.request_identity_sha256 == identity.canonical_hash
    assert observation.window_manifest_sha256 == manifest.canonical_hash
    assert observation.source_interval.coarse_range == TickRange(1_037, 1_063)
    assert observation.source_interval.mapping_error_bound_source_pts == 1
    assert observation.source_interval.provider_uncertainty_proxy_pts == 2
    assert observation.source_interval.source_time_base == manifest.source_time_base
    assert observation.source_interval.proxy_time_base == manifest.timeline_map.proxy_time_base
    assert observation.core_owned
    assert result.to_mapping()["observations"][0]["source_interval"]["semantic_precision"] == "coarse_only"  # type: ignore[index]


def test_parser_retains_context_observation_until_manifest_set_assigns_unique_owner() -> None:
    manifest, manifest_set, policy, identity = _context()
    context_only = _observation(
        manifest,
        proxy_interval={"start_pts": 0, "end_pts": 9, "uncertainty_pts": 1},
        supporting_frame_ids=[manifest.frame_samples[0].frame_id],
    )

    parsed = parse_vlm_response(
        _raw({"schema_version": 1, "observations": [context_only]}),
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=policy,
    ).observations[0]

    assert not parsed.core_owned
    owner = select_core_owner(manifest_set, parsed.source_interval.coarse_range)
    assert owner != manifest
    assert owner.core_range == TickRange(1_000, 1_010)
    owner_identity = VlmRequestIdentity.from_manifest(
        owner,
        manifest_set,
        prompt_template_sha256=_hash("1"),
        prompt_version="story-evidence-v1",
        response_schema_sha256=_hash("2"),
        model_id="vlm-model-v1",
        provider_id="fake-provider",
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        parse_policy=policy,
    )
    owned = parse_vlm_response(
        _raw({"schema_version": 1, "observations": [context_only]}),
        manifest=owner,
        manifest_set=manifest_set,
        request_identity=owner_identity,
        policy=policy,
    ).observations[0]
    assert [parsed.core_owned, owned.core_owned] == [False, True]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"extra": "not allowed"}, "UNKNOWN_RESPONSE_FIELD"),
        ({"provenance": {"window_manifest_sha256": _hash("0")}}, "UNKNOWN_RESPONSE_FIELD"),
        ({"path": "/tmp/video.mp4"}, "UNKNOWN_RESPONSE_FIELD"),
        ({"recipe": {"publish": True}}, "UNKNOWN_RESPONSE_FIELD"),
    ],
)
def test_parser_rejects_extra_physical_decision_and_forged_provenance_fields(
    mutation: dict[str, object], code: str
) -> None:
    manifest, manifest_set, policy, identity = _context()
    item = _observation(manifest, **mutation)
    with pytest.raises(VlmResponseRejected) as raised:
        parse_vlm_response(
            _raw({"schema_version": 1, "observations": [item]}),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )
    assert raised.value.code == code


def test_parser_rejects_unknown_frame_and_frame_interval_mismatch() -> None:
    manifest, manifest_set, policy, identity = _context()
    unknown = _observation(manifest, supporting_frame_ids=[_hash("9")])
    with pytest.raises(VlmResponseRejected) as raised:
        parse_vlm_response(
            _raw({"schema_version": 1, "observations": [unknown]}),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )
    assert raised.value.code == "UNKNOWN_FRAME_ID"

    mismatched = _observation(
        manifest,
        proxy_interval={"start_pts": 0, "end_pts": 20, "uncertainty_pts": 0},
        supporting_frame_ids=[manifest.frame_samples[2].frame_id],
    )
    with pytest.raises(VlmResponseRejected) as raised:
        parse_vlm_response(
            _raw({"schema_version": 1, "observations": [mismatched]}),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )
    assert raised.value.code == "FRAME_INTERVAL_MISMATCH"


def test_parser_expands_only_a_unique_allowlisted_sha256_prefix() -> None:
    manifest, manifest_set, policy, identity = _context()
    canonical_id = manifest.frame_samples[1].frame_id
    unique_160_bit_prefix = canonical_id[: len("sha256:") + 40]

    parsed = parse_vlm_response(
        _raw(
            {
                "schema_version": 1,
                "observations": [
                    _observation(manifest, supporting_frame_ids=[unique_160_bit_prefix])
                ],
            }
        ),
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=policy,
    )

    assert parsed.observations[0].supporting_frame_ids == (canonical_id,)


def test_parser_rejects_a_frame_prefix_shorter_than_160_bits() -> None:
    manifest, manifest_set, policy, identity = _context()
    short_prefix = manifest.frame_samples[1].frame_id[: len("sha256:") + 39]

    with pytest.raises(VlmResponseRejected) as raised:
        parse_vlm_response(
            _raw(
                {
                    "schema_version": 1,
                    "observations": [
                        _observation(manifest, supporting_frame_ids=[short_prefix])
                    ],
                }
            ),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )

    assert raised.value.code == "UNKNOWN_FRAME_ID"


def test_parser_rechecks_directly_constructed_request_identity_fields() -> None:
    manifest, manifest_set, policy, identity = _context()
    forged_identities = (
        ("source_sha256", replace(identity, source_sha256=_hash("9"))),
        (
            "frame_pts_index_set_sha256",
            replace(identity, frame_pts_index_set_sha256=_hash("9")),
        ),
        (
            "window_manifest_set_sha256",
            replace(identity, window_manifest_set_sha256=_hash("9")),
        ),
    )

    for field_name, forged in forged_identities:
        with pytest.raises(VlmValidationError, match=field_name):
            parse_vlm_response(
                _raw({"schema_version": 1, "observations": [_observation(manifest)]}),
                manifest=manifest,
                manifest_set=manifest_set,
                request_identity=forged,
                policy=policy,
            )


@pytest.mark.parametrize(
    "proxy_interval",
    [
        {"start_pts": True, "end_pts": 60, "uncertainty_pts": 0},
        {"start_pts": 40.5, "end_pts": 60, "uncertainty_pts": 0},
        {"start_pts": 40, "end_pts": 60, "uncertainty_pts": False},
        {"start_pts": -1, "end_pts": 20, "uncertainty_pts": 0},
        {"start_pts": 90, "end_pts": 101, "uncertainty_pts": 0},
    ],
)
def test_parser_rejects_bool_float_and_out_of_bounds_ticks(proxy_interval: dict[str, object]) -> None:
    manifest, manifest_set, policy, identity = _context()
    item = _observation(manifest, proxy_interval=proxy_interval)
    with pytest.raises(VlmResponseRejected):
        parse_vlm_response(
            _raw({"schema_version": 1, "observations": [item]}),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b'{"schema_version":1,"observations":',
        b'{"schema_version":1,"schema_version":1,"observations":[]}',
        b'{"schema_version":1,"observations":[],"x":NaN}',
        b"\xff",
    ],
)
def test_parser_rejects_invalid_non_finite_duplicate_or_non_utf8_json(raw: bytes) -> None:
    manifest, manifest_set, policy, identity = _context()
    with pytest.raises(VlmResponseRejected):
        parse_vlm_response(
            raw,
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )


def test_low_confidence_and_budgets_are_explicitly_indeterminate() -> None:
    manifest, manifest_set, policy, identity = _context()
    low = _observation(manifest, confidence="0.79")
    with pytest.raises(VlmResponseIndeterminate) as raised:
        parse_vlm_response(
            _raw({"schema_version": 1, "observations": [low]}),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )
    assert raised.value.code == "LOW_CONFIDENCE"

    many = [_observation(manifest, summary=f"observation {index}") for index in range(5)]
    with pytest.raises(VlmResponseIndeterminate) as raised:
        parse_vlm_response(
            _raw({"schema_version": 1, "observations": many}),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )
    assert raised.value.code == "OBSERVATION_BUDGET_EXCEEDED"

    tiny_policy = VlmParsePolicy(Decimal("0.80"), 20, 4, 128, 256)
    tiny_identity = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=_hash("1"),
        prompt_version="story-evidence-v1",
        response_schema_sha256=_hash("2"),
        model_id="vlm-model-v1",
        provider_id="fake-provider",
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        parse_policy=tiny_policy,
    )
    with pytest.raises(VlmResponseIndeterminate) as raised:
        parse_vlm_response(
            _raw({"schema_version": 1, "observations": [_observation(manifest)]}),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=tiny_identity,
            policy=tiny_policy,
        )
    assert raised.value.code == "RESPONSE_BUDGET_EXCEEDED"


def test_parser_rejects_whitespace_only_summary() -> None:
    manifest, manifest_set, policy, identity = _context()
    blank = _observation(manifest, summary="   \t  ")
    with pytest.raises(VlmResponseRejected) as raised:
        parse_vlm_response(
            _raw({"schema_version": 1, "observations": [blank]}),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )
    assert raised.value.code == "INVALID_SUMMARY"


def test_response_order_does_not_change_derived_observation_identity_order() -> None:
    manifest, manifest_set, policy, identity = _context()
    first = _observation(manifest, kind="observation", summary="First registered visual observation.")
    second = _observation(manifest, kind="relation", summary="Second registered visual relation.")
    forward = parse_vlm_response(
        _raw({"schema_version": 1, "observations": [first, second]}),
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=policy,
    )
    reverse = parse_vlm_response(
        _raw({"schema_version": 1, "observations": [second, first]}),
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=policy,
    )

    assert tuple(item.observation_id for item in forward.observations) == tuple(
        item.observation_id for item in reverse.observations
    )
