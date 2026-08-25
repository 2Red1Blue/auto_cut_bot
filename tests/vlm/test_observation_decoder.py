from __future__ import annotations

from copy import deepcopy

import pytest
from autocut_kernel.vlm import (
    VlmValidationError,
    decode_vlm_semantic_pack,
    parse_vlm_response,
)

from .test_parser import _context, _payload, _raw


def _mapping() -> dict[str, object]:
    manifest, manifest_set, policy, identity = _context()
    pack = parse_vlm_response(
        _raw(_payload(manifest)),
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=policy,
    )
    return pack.to_mapping()


def test_decodes_exact_committed_v3_mapping_and_replays_canonically() -> None:
    mapping = _mapping()

    decoded = decode_vlm_semantic_pack(mapping)

    assert decoded.to_mapping() == mapping
    assert decoded.canonical_hash.startswith("sha256:")


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda value: value.update(source_id="forged"), "closed schema"),
        (lambda value: value.update(schema_version=2), "integer 3"),
        (
            lambda value: value["facts"][0].update(fact_id="sha256:" + "0" * 64),
            "global ID",
        ),
        (
            lambda value: value["facts"][0]["support"].update(confidence=0.04),
            "decimal string",
        ),
        (
            lambda value: value["facts"][0]["support"]["proxy_interval"].update(start_pts=True),
            "integer PTS tick",
        ),
        (
            lambda value: value["facts"][0]["support"].update(
                supporting_frame_ids=[
                    value["facts"][0]["support"]["supporting_frame_ids"][0],
                    value["facts"][0]["support"]["supporting_frame_ids"][0],
                ]
            ),
            "sorted and unique",
        ),
        (
            lambda value: value["candidate_hypotheses"][0].update(
                editing_modes=["action", "dialogue"]
            ),
            "canonical order",
        ),
    ],
)
def test_decoder_rejects_forged_underived_or_noncanonical_values(mutation, match: str) -> None:
    mapping = deepcopy(_mapping())
    mutation(mapping)

    with pytest.raises(VlmValidationError, match=match):
        decode_vlm_semantic_pack(mapping)


def test_decoder_rechecks_closed_references() -> None:
    mapping = _mapping()
    mapping["events"][0]["fact_refs"] = ["sha256:" + "9" * 64]

    with pytest.raises(VlmValidationError, match="event fact reference is not closed"):
        decode_vlm_semantic_pack(mapping)


def test_decoder_cannot_bypass_causal_self_loop_validation() -> None:
    mapping = _mapping()
    event = mapping["events"][0]
    event_id = event["event_id"]
    event["cause_event_refs"] = [event_id]
    event["effect_event_refs"] = [event_id]

    with pytest.raises(VlmValidationError, match="self-loops"):
        decode_vlm_semantic_pack(mapping)


def test_decoder_cannot_bypass_candidate_support_overlap_validation() -> None:
    mapping = _mapping()
    support = mapping["candidate_hypotheses"][0]["support"]
    support["source_interval"]["coarse_range"].update(start_pts=1_000, end_pts=1_020)

    with pytest.raises(VlmValidationError, match="overlap anchor, supporting, and payoff"):
        decode_vlm_semantic_pack(mapping)


def test_decoder_cannot_bypass_continuity_flag_validation() -> None:
    mapping = _mapping()
    mapping["continuity"]["starts_mid_event"] = True

    with pytest.raises(VlmValidationError, match="starts_mid_event"):
        decode_vlm_semantic_pack(mapping)
