from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest
from autocut_kernel.media import TickRange, TimeBase
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.vlm import (
    MappedSourceInterval,
    VlmObservation,
    VlmObservationKind,
    VlmObservationSet,
    VlmValidationError,
    decode_vlm_observation_set,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
FRAME_A = "sha256:" + "d" * 64


def _mapping() -> dict[str, object]:
    interval = MappedSourceInterval(
        coarse_range=TickRange(100, 220),
        mapping_error_bound_source_pts=2,
        source_time_base=TimeBase(1, 1_000),
        provider_uncertainty_proxy_pts=3,
        proxy_time_base=TimeBase(1, 100),
    )
    identity_payload = {
        "confidence": "0.90",
        "kind": "change",
        "request_identity_sha256": HASH_A,
        "source_interval": interval.to_mapping(),
        "summary": "角色发现关键证据",
        "supporting_frame_ids": [FRAME_A],
    }
    observation = VlmObservation(
        observation_id=canonical_sha256(identity_payload),
        kind=VlmObservationKind.CHANGE,
        summary="角色发现关键证据",
        confidence=Decimal("0.90"),
        supporting_frame_ids=(FRAME_A,),
        source_interval=interval,
        request_identity_sha256=HASH_A,
        window_manifest_sha256=HASH_B,
        core_owned=True,
    )
    return VlmObservationSet(HASH_A, HASH_B, HASH_C, (observation,)).to_mapping()


def test_decodes_exact_committed_mapping_and_replays_canonically() -> None:
    mapping = _mapping()

    decoded = decode_vlm_observation_set(mapping)

    assert decoded.to_mapping() == mapping
    assert decoded.canonical_hash == canonical_sha256(mapping)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda value: value.update(extra=True), "closed schema"),
        (
            lambda value: value["observations"][0].update(observation_id=HASH_C),
            "not derivable",
        ),
        (
            lambda value: value["observations"][0].update(confidence=0.9),
            "decimal string",
        ),
        (
            lambda value: value["observations"][0]["source_interval"][
                "mapping_error_bound"
            ].update(tick=True),
            "integer PTS tick",
        ),
        (
            lambda value: value["observations"][0]["source_interval"][
                "mapping_error_bound"
            ].update(clock="proxy"),
            "clock labels",
        ),
        (
            lambda value: value["observations"][0].update(
                supporting_frame_ids=[FRAME_A, FRAME_A]
            ),
            "sorted, and unique",
        ),
    ],
)
def test_rejects_noncanonical_or_underived_persisted_values(mutation, match: str) -> None:
    mapping = deepcopy(_mapping())
    mutation(mapping)

    with pytest.raises(VlmValidationError, match=match):
        decode_vlm_observation_set(mapping)


def test_observation_set_provenance_must_match_every_child() -> None:
    mapping = _mapping()
    mapping["provenance"]["request_identity_sha256"] = HASH_C

    with pytest.raises(VlmValidationError, match="exact request identity"):
        decode_vlm_observation_set(mapping)
