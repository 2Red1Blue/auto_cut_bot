from __future__ import annotations

import json
from uuid import UUID

import pytest
from autocut_kernel.pipeline.edit_proposal import (
    EDIT_PROPOSAL_SCHEMA_VERSION,
    EditProposal,
    EditProposalActor,
    EditProposalError,
    SelectVariantOperation,
    decode_edit_proposal,
    decode_edit_proposal_json,
    encode_edit_proposal_json,
)
from autocut_kernel.store.models import ArtifactScope, CommittedArtifactMemberReference


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _recipe_ref() -> CommittedArtifactMemberReference:
    return CommittedArtifactMemberReference(
        UUID("11111111-1111-4111-8111-111111111111"),
        UUID("22222222-2222-4222-8222-222222222222"),
        1,
        ArtifactScope("pipeline", "job", "r4b-edit-proposal"),
        "recipe",
        "production_recipe@story-1",
        7,
        _hash("a"),
    )


def _variant_set_ref() -> CommittedArtifactMemberReference:
    return CommittedArtifactMemberReference(
        UUID("33333333-3333-4333-8333-333333333333"),
        UUID("44444444-4444-4444-8444-444444444444"),
        0,
        ArtifactScope("pipeline", "job", "r4b-edit-proposal"),
        "span_variant_set",
        "span_variant_set",
        7,
        _hash("b"),
    )


def _proposal(*, created_at: str = "2026-09-09T00:00:00Z") -> EditProposal:
    return EditProposal(
        UUID("55555555-5555-4555-8555-555555555555"),
        _recipe_ref(),
        7,
        _hash("c"),
        (SelectVariantOperation(_variant_set_ref(), _hash("d")),),
        "Replace the proven span with the reviewed alternative.",
        EditProposalActor("human", "operator-42"),
        created_at,
    )


def test_round_trip_is_closed_and_created_at_is_audit_only() -> None:
    first = _proposal()
    later = _proposal(created_at="2026-09-09T00:00:01Z")

    encoded = encode_edit_proposal_json(first)
    decoded = decode_edit_proposal_json(encoded)

    assert decoded == first
    assert encoded == encode_edit_proposal_json(first)
    assert first.idempotency_key == "edit-proposal:55555555-5555-4555-8555-555555555555"
    assert first.canonical_hash == later.canonical_hash
    assert first.to_mapping()["created_at"] != later.to_mapping()["created_at"]
    assert decoded.edit_ops[0].span_variant_set_content_hash == _hash("b")


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("video_in_tick", 42, "unknown fields"),
        ("ffmpeg_args", ["-vf", "trim"], "unknown fields"),
        ("legacy_edit_id", "old-1", "unknown fields"),
        ("operation", "reorder_beat", "only select_variant"),
    ],
)
def test_decoder_rejects_every_non_select_variant_escape(
    field: str,
    value: object,
    match: str,
) -> None:
    payload = _proposal().to_mapping()
    operation = payload["edit_ops"][0]
    assert isinstance(operation, dict)
    operation[field] = value

    with pytest.raises(EditProposalError, match=match):
        decode_edit_proposal(payload)


def test_decoder_rejects_unbound_or_inconsistent_persisted_identities() -> None:
    payload = _proposal().to_mapping()
    payload["base_revision"] = 8

    with pytest.raises(EditProposalError, match="base_revision differs"):
        decode_edit_proposal(payload)

    payload = _proposal().to_mapping()
    operation = payload["edit_ops"][0]
    assert isinstance(operation, dict)
    reference = operation["span_variant_set_ref"]
    assert isinstance(reference, dict)
    reference["artifact_type"] = "recipe"

    with pytest.raises(EditProposalError, match="canonical SpanVariantSet member"):
        decode_edit_proposal(payload)


def test_proposal_rejects_multiple_variant_choices() -> None:
    payload = _proposal().to_mapping()
    operations = payload["edit_ops"]
    assert isinstance(operations, list)
    operations.append(dict(operations[0]))

    with pytest.raises(EditProposalError, match="exactly one select_variant"):
        decode_edit_proposal(payload)


def test_json_codec_rejects_duplicate_keys_and_noncanonical_audit_timestamp() -> None:
    raw = encode_edit_proposal_json(_proposal())
    duplicate = raw.replace(b'"proposal_id":', b'"proposal_id":"x","proposal_id":', 1)

    with pytest.raises(EditProposalError, match="duplicate JSON key"):
        decode_edit_proposal_json(duplicate)

    payload = json.loads(raw)
    payload["created_at"] = "2026-09-09T08:00:00+08:00"
    with pytest.raises(EditProposalError, match="UTC Z suffix"):
        decode_edit_proposal_json(json.dumps(payload).encode())


def test_schema_version_remains_the_only_allowed_document_version() -> None:
    payload = _proposal().to_mapping()
    assert payload["schema_version"] == EDIT_PROPOSAL_SCHEMA_VERSION
    payload["schema_version"] = "edit-proposal-v0"

    with pytest.raises(EditProposalError, match="schema is unsupported"):
        decode_edit_proposal(payload)
