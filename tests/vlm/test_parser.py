from __future__ import annotations

import json
from copy import deepcopy

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    ProxyTimelineSegment,
    VlmParsePolicy,
    VlmRequestIdentity,
    VlmResponseIndeterminate,
    VlmResponseRejected,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
    parse_vlm_response,
)

from . import frame_pts_set


def _hash(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _context(*, piecewise: bool = False):
    time_base = TimeBase(1, 1_000)
    timeline = (
        ProxyTimelineMap(
            proxy_time_base=time_base,
            source_time_base=time_base,
            segments=(
                ProxyTimelineSegment(TickRange(0, 50), TickRange(1_000, 1_040), 1),
                ProxyTimelineSegment(TickRange(50, 100), TickRange(1_040, 1_100), 2),
            ),
            certificate_kind="piecewise_monotonic",
        )
        if piecewise
        else ProxyTimelineMap.translation(
            time_base=time_base,
            proxy_range=TickRange(0, 100),
            source_start_pts=1_000,
            max_source_error_pts=1,
        )
    )
    source_ticks = (1_008, 1_040, 1_088) if piecewise else (1_010, 1_050, 1_090)
    manifest = WindowManifest(
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
            ticks=source_ticks,
        ),
        proxy_blob_ref=WindowProxyBlobRef("proxy-001", _hash("b"), 4_096, "video/mp4"),
        preprocess_policy_sha256=_hash("c"),
        window_sampling_policy_sha256=_hash("4"),
        timeline_map=timeline,
        frame_samples=tuple(
            WindowFrameSample(source_pts, proxy_pts, _hash(digit))
            for source_pts, proxy_pts, digit in zip(source_ticks, (10, 50, 90), "def", strict=True)
        ),
    )
    manifest_set = WindowManifestSet(
        manifest.source_id,
        manifest.source_clock_id,
        manifest.source_sha256,
        manifest.stream_index,
        manifest.source_time_base,
        manifest.core_range,
        (manifest,),
    )
    policy = VlmParsePolicy(
        max_response_bytes=64_000,
        max_entities=8,
        max_facts=16,
        max_events=16,
        max_candidate_hypotheses=8,
        max_temporal_segments=8,
        max_measurements=16,
        max_text_characters=512,
        max_total_text_characters=8_192,
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
        parse_policy=policy,
    )
    return manifest, manifest_set, policy, identity


def _support(manifest: WindowManifest, *, confidence: str = "0.90") -> dict[str, object]:
    return {
        "proxy_interval": {"start_pts": 40, "end_pts": 60, "uncertainty_pts": 2},
        "supporting_frame_ids": [manifest.frame_samples[1].frame_id],
        "confidence": confidence,
    }


def _payload(manifest: WindowManifest, *, candidate_kind: str = "highlight") -> dict[str, object]:
    payoff_refs = ["event_1"] if candidate_kind == "highlight" else []
    open_question = None if candidate_kind == "highlight" else "Who is behind the door?"
    return {
        "schema_version": 3,
        "window_summary": {
            "summary": "A person discovers visible evidence.",
            "dominant_temporal_mode": "present",
            "fact_refs": ["fact_1"],
            "event_refs": ["event_1"],
            "confidence": "0.01",
        },
        "continuity": {
            "starts_mid_event": False,
            "ends_mid_event": True,
            "continues_from_previous": False,
            "continues_into_next": True,
            "entry_state_fact_refs": [],
            "exit_state_fact_refs": ["fact_1"],
            "temporal_segments": [
                {
                    "proxy_interval": _support(manifest)["proxy_interval"],
                    "mode": "present",
                    "summary": "The discovery occurs in the present.",
                    "supporting_frame_ids": _support(manifest)["supporting_frame_ids"],
                    "confidence": "0.02",
                }
            ],
        },
        "entities": [
            {
                "local_entity_id": "entity_1",
                "entity_kind": "person",
                "display_label": "Visible person",
                "visual_description": "A person wearing a dark coat.",
                "support": _support(manifest, confidence="0.03"),
            }
        ],
        "facts": [
            {
                "local_fact_id": "fact_1",
                "fact_kind": "visible_action",
                "subject_ref": "entity_1",
                "object_ref": None,
                "summary": "The person opens a box.",
                "support": _support(manifest, confidence="0.04"),
            }
        ],
        "events": [
            {
                "local_event_id": "event_1",
                "event_kind": "reveal",
                "summary": "The box reveals a marked key.",
                "participant_refs": ["entity_1"],
                "fact_refs": ["fact_1"],
                "cause_event_refs": [],
                "effect_event_refs": [],
                "open_question": None,
                "temporal_mode": "present",
                "support": _support(manifest, confidence="0.05"),
            }
        ],
        "candidate_hypotheses": [
            {
                "local_candidate_id": "candidate_1",
                "candidate_kind": candidate_kind,
                "anchor_event_ref": "event_1",
                "supporting_event_refs": ["event_1"],
                "context_event_refs": [],
                "payoff_event_refs": payoff_refs,
                "open_question": open_question,
                "reason": "The reveal changes the viewer's understanding.",
                "anchor_summary": "A marked key is revealed.",
                "payoff_or_open_question": "The key raises a concrete mystery.",
                "dialogue_excerpt": None,
                "editing_modes": ["dialogue", "action"],
                "narrative_functions": ["hook", "reveal", "payoff"],
                "tags": ["dialogue", "emotion", "reveal"],
                "measurements": [
                    {
                        "measurement_kind": "reveal_strength",
                        "value": "0.90",
                        "confidence": "0.06",
                        "fact_refs": ["fact_1"],
                        "event_refs": ["event_1"],
                    }
                ],
                "support": _support(manifest, confidence="0.07"),
            }
        ],
    }


def _interval_support(
    manifest: WindowManifest, start_pts: int, end_pts: int, frame_position: int
) -> dict[str, object]:
    return {
        "proxy_interval": {
            "start_pts": start_pts,
            "end_pts": end_pts,
            "uncertainty_pts": 0,
        },
        "supporting_frame_ids": [manifest.frame_samples[frame_position].frame_id],
        "confidence": "0.50",
    }


def _append_fact_event(
    payload: dict[str, object],
    manifest: WindowManifest,
    suffix: int,
    *,
    support: dict[str, object] | None = None,
) -> None:
    fact = deepcopy(payload["facts"][0])
    fact["local_fact_id"] = f"fact_{suffix}"
    fact["summary"] = f"Visible fact {suffix}."
    fact["support"] = support or _support(manifest)
    payload["facts"].append(fact)
    event = deepcopy(payload["events"][0])
    event["local_event_id"] = f"event_{suffix}"
    event["summary"] = f"Visible event {suffix}."
    event["fact_refs"] = [f"fact_{suffix}"]
    event["cause_event_refs"] = []
    event["effect_event_refs"] = []
    event["support"] = support or _support(manifest)
    payload["events"].append(event)


def _raw(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _parse(payload: object, *, piecewise: bool = False):
    manifest, manifest_set, policy, identity = _context(piecewise=piecewise)
    return parse_vlm_response(
        _raw(payload),
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=policy,
    )


def test_parses_v3_and_derives_mapping_global_ids_owner_and_provenance() -> None:
    manifest, manifest_set, policy, identity = _context(piecewise=True)
    raw = _raw(_payload(manifest))

    pack = parse_vlm_response(
        raw,
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=policy,
    )

    fact = pack.facts[0]
    assert pack.request_identity_sha256 == identity.canonical_hash
    assert pack.window_manifest_sha256 == manifest.canonical_hash
    assert pack.raw_response_sha256.startswith("sha256:")
    assert fact.fact_id.startswith("sha256:")
    assert fact.subject_ref == pack.entities[0].entity_id
    assert fact.support.source_interval.coarse_range == TickRange(1_029, 1_057)
    assert fact.support.core_owner_window_manifest_sha256 == manifest.canonical_hash
    assert pack.window_summary.fact_refs == (fact.fact_id,)
    assert pack.to_mapping()["schema_version"] == 3


def test_low_confidence_is_preserved_and_candidate_array_may_be_empty() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    payload["candidate_hypotheses"] = []

    pack = _parse(payload)

    assert pack.facts[0].support.confidence.is_zero() is False
    assert str(pack.facts[0].support.confidence) == "0.04"
    assert pack.candidate_hypotheses == ()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(source_id="forged"),
        lambda payload: payload.update(artifact_ref="sha256:" + "0" * 64),
        lambda payload: payload.update(asr="forged transcript"),
        lambda payload: payload.update(vad={"speech": True}),
        lambda payload: payload["facts"][0].update(source_interval={}),
        lambda payload: payload["events"][0].update(start_pts=10, end_pts=20),
    ],
)
def test_closed_schema_rejects_provider_forged_authority_and_physical_fields(mutation) -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    mutation(payload)

    with pytest.raises(VlmResponseRejected) as raised:
        _parse(payload)
    assert raised.value.code == "UNKNOWN_RESPONSE_FIELD"


def test_rejects_duplicate_json_keys_v2_and_missing_required_root_fields() -> None:
    manifest, manifest_set, policy, identity = _context()
    with pytest.raises(VlmResponseRejected, match="DUPLICATE_JSON_KEY"):
        parse_vlm_response(
            b'{"schema_version":3,"schema_version":3}',
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=policy,
        )
    with pytest.raises(VlmResponseRejected, match="UNSUPPORTED_SCHEMA_VERSION"):
        _parse(
            {
                "schema_version": 2,
                **{k: v for k, v in _payload(manifest).items() if k != "schema_version"},
            }
        )
    incomplete = _payload(manifest)
    del incomplete["continuity"]
    with pytest.raises(VlmResponseRejected, match="MISSING_RESPONSE_FIELD"):
        _parse(incomplete)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda payload: payload["facts"].append(deepcopy(payload["facts"][0])),
            "DUPLICATE_LOCAL_ID",
        ),
        (
            lambda payload: payload["events"][0].update(fact_refs=["fact_1", "fact_1"]),
            "DUPLICATE_REFERENCE",
        ),
        (lambda payload: payload["facts"][0].update(subject_ref="missing"), "UNKNOWN_REFERENCE"),
        (
            lambda payload: payload["events"][0].update(cause_event_refs=["missing"]),
            "UNKNOWN_REFERENCE",
        ),
        (
            lambda payload: payload["candidate_hypotheses"][0].update(anchor_event_ref="missing"),
            "UNKNOWN_REFERENCE",
        ),
        (
            lambda payload: payload["candidate_hypotheses"][0]["measurements"][0].update(
                fact_refs=[], event_refs=[]
            ),
            "EMPTY_MEASUREMENT_SUPPORT",
        ),
    ],
)
def test_local_ids_and_all_reference_layers_are_unique_and_closed(mutation, code: str) -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    mutation(payload)
    with pytest.raises(VlmResponseRejected) as raised:
        _parse(payload)
    assert raised.value.code == code


def test_event_fact_support_mismatch_is_a_closed_response_rejection() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    payload["events"][0]["support"]["proxy_interval"].update(
        start_pts=70,
        end_pts=95,
    )
    payload["events"][0]["support"]["supporting_frame_ids"] = [
        manifest.frame_samples[2].frame_id
    ]

    with pytest.raises(VlmResponseRejected) as raised:
        _parse(payload)

    assert raised.value.code == "SEMANTIC_PACK_INVARIANT_VIOLATION"
    assert "event support" in str(raised.value)


@pytest.mark.parametrize("bad_pts", [True, 40.5, "40", None])
def test_proxy_pts_are_strict_json_integers(bad_pts: object) -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    payload["facts"][0]["support"]["proxy_interval"]["start_pts"] = bad_pts
    with pytest.raises(VlmResponseRejected) as raised:
        _parse(payload)
    assert raised.value.code == "INVALID_PTS"


@pytest.mark.parametrize("bad_decimal", [0.9, 1, "9e-1", "+0.9", "00.9", "1.1"])
def test_confidence_and_measurement_values_are_canonical_decimal_strings(
    bad_decimal: object,
) -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    payload["facts"][0]["support"]["confidence"] = bad_decimal
    with pytest.raises(VlmResponseRejected) as raised:
        _parse(payload)
    assert raised.value.code == "INVALID_DECIMAL"


def test_frame_ids_require_full_exact_sha256_allowlist_membership() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    full = manifest.frame_samples[1].frame_id
    payload["facts"][0]["support"]["supporting_frame_ids"] = [full[:-8]]
    with pytest.raises(VlmResponseRejected) as raised:
        _parse(payload)
    assert raised.value.code == "UNKNOWN_FRAME_ID"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda candidate: candidate.update(editing_modes=["action", "dialogue"]),
        lambda candidate: candidate.update(narrative_functions=["payoff", "hook"]),
        lambda candidate: candidate.update(tags=["emotion", "dialogue"]),
        lambda candidate: candidate.update(tags=["unregistered"]),
    ],
)
def test_candidate_closed_sets_are_nonempty_unique_and_canonically_ordered(mutation) -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    mutation(payload["candidate_hypotheses"][0])
    with pytest.raises(VlmResponseRejected):
        _parse(payload)


def test_hook_and_highlight_rules_are_fail_closed() -> None:
    manifest, _, _, _ = _context()
    assert _parse(_payload(manifest, candidate_kind="hook")).candidate_hypotheses[0].open_question

    bad_hook = _payload(manifest, candidate_kind="hook")
    bad_hook["candidate_hypotheses"][0]["open_question"] = None
    with pytest.raises(VlmResponseRejected, match="hook requires"):
        _parse(bad_hook)

    bad_highlight = _payload(manifest)
    bad_highlight["candidate_hypotheses"][0]["payoff_event_refs"] = []
    with pytest.raises(VlmResponseRejected, match="highlight requires"):
        _parse(bad_highlight)


def test_causal_graph_rejects_self_loop_even_when_inverse_fields_match() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    event = payload["events"][0]
    event["cause_event_refs"] = ["event_1"]
    event["effect_event_refs"] = ["event_1"]

    with pytest.raises(ValueError, match="self-loops"):
        _parse(payload)


def test_causal_graph_rejects_asymmetric_inverse_edges() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    _append_fact_event(payload, manifest, 2)
    payload["events"][0]["effect_event_refs"] = ["event_2"]

    with pytest.raises(ValueError, match="mutually inverse"):
        _parse(payload)


def test_causal_graph_rejects_longer_directed_cycle_with_symmetric_edges() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    _append_fact_event(payload, manifest, 2)
    _append_fact_event(payload, manifest, 3)
    first, second, third = payload["events"]
    first.update(cause_event_refs=["event_3"], effect_event_refs=["event_2"])
    second.update(cause_event_refs=["event_1"], effect_event_refs=["event_3"])
    third.update(cause_event_refs=["event_2"], effect_event_refs=["event_1"])

    with pytest.raises(ValueError, match="acyclic"):
        _parse(payload)


def test_event_support_must_overlap_every_direct_fact_support() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    payload["facts"][0]["support"] = _interval_support(manifest, 0, 20, 0)
    payload["events"][0]["support"] = _interval_support(manifest, 80, 100, 2)

    with pytest.raises(ValueError, match="overlap every directly referenced fact"):
        _parse(payload)


def test_measurements_must_remain_inside_candidate_event_and_fact_closure() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    _append_fact_event(payload, manifest, 2)
    measurement = payload["candidate_hypotheses"][0]["measurements"][0]
    measurement.update(fact_refs=["fact_2"], event_refs=["event_2"])

    with pytest.raises(ValueError, match="candidate semantic closure"):
        _parse(payload)


def test_candidate_support_must_overlap_anchor_supporting_and_payoff_events() -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    late = _interval_support(manifest, 80, 100, 2)
    payload["facts"][0]["support"] = late
    payload["events"][0]["support"] = late
    payload["candidate_hypotheses"][0]["support"] = _interval_support(manifest, 0, 20, 0)

    with pytest.raises(ValueError, match="overlap anchor, supporting, and payoff"):
        _parse(payload)


@pytest.mark.parametrize(
    "segments",
    [
        ((80, 100, 2), (0, 20, 0)),
        ((0, 60, 0), (40, 80, 1)),
    ],
)
def test_continuity_segments_are_sorted_and_non_overlapping(segments) -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    payload["continuity"]["temporal_segments"] = [
        {
            "proxy_interval": _interval_support(manifest, start, end, frame)["proxy_interval"],
            "mode": "present",
            "summary": f"Segment {position}.",
            "supporting_frame_ids": _interval_support(manifest, start, end, frame)[
                "supporting_frame_ids"
            ],
            "confidence": "0.50",
        }
        for position, (start, end, frame) in enumerate(segments)
    ]

    with pytest.raises(ValueError, match="canonical proxy interval order|must not overlap"):
        _parse(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_state_fact_refs": ["fact_1"]},
        {
            "continues_from_previous": True,
            "entry_state_fact_refs": ["fact_1"],
        },
        {
            "starts_mid_event": True,
            "continues_from_previous": True,
            "entry_state_fact_refs": [],
        },
        {
            "ends_mid_event": False,
            "continues_into_next": False,
            "exit_state_fact_refs": ["fact_1"],
        },
        {"ends_mid_event": False},
        {"continues_into_next": True, "ends_mid_event": True, "exit_state_fact_refs": []},
    ],
)
def test_continuity_flags_and_boundary_state_refs_cannot_contradict(changes) -> None:
    manifest, _, _, _ = _context()
    payload = _payload(manifest)
    payload["continuity"].update(changes)

    with pytest.raises(VlmResponseRejected, match="continuity") as raised:
        _parse(payload)

    assert raised.value.code == "SEMANTIC_PACK_INVARIANT_VIOLATION"


def test_structural_and_byte_budgets_are_indeterminate_not_empty_success() -> None:
    manifest, manifest_set, policy, identity = _context()
    payload = _payload(manifest)
    payload["facts"].append({**deepcopy(payload["facts"][0]), "local_fact_id": "fact_2"})
    tight = VlmParsePolicy(**{**policy.to_mapping(), "max_facts": 1})
    tight_identity = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=_hash("1"),
        prompt_version="semantic-pack-v3",
        response_schema_sha256=_hash("2"),
        model_id="vlm-v3",
        provider_id="provider",
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        parse_policy=tight,
    )
    with pytest.raises(VlmResponseIndeterminate, match="STRUCTURE_BUDGET_EXCEEDED"):
        parse_vlm_response(
            _raw(payload),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=tight_identity,
            policy=tight,
        )

    byte_tight = VlmParsePolicy(**{**policy.to_mapping(), "max_response_bytes": 10})
    byte_identity = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=_hash("1"),
        prompt_version="semantic-pack-v3",
        response_schema_sha256=_hash("2"),
        model_id="vlm-v3",
        provider_id="provider",
        request_parameters_sha256=_hash("3"),
        request_payload_sha256=_hash("4"),
        parse_policy=byte_tight,
    )
    with pytest.raises(VlmResponseIndeterminate, match="RESPONSE_BUDGET_EXCEEDED"):
        parse_vlm_response(
            _raw(_payload(manifest)),
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=byte_identity,
            policy=byte_tight,
        )
