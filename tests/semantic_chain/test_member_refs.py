"""Pure identity values: synthetic references do not establish commitment."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest
from autocut_kernel.semantic_chain.member_refs import (
    SemanticMemberIdentity,
    SemanticObjectRef,
    SemanticReferenceError,
)
from autocut_kernel.store import ArtifactMember, ArtifactScope, CommittedArtifactMemberReference
from autocut_kernel.store.models import canonical_payload_hash

SCOPE = ArtifactScope("pipeline", "job", "semantic-unit")
PAYLOAD = '{"事实":"找到钥匙","count":1}'
HASH = canonical_payload_hash(PAYLOAD)
IDENTITY = SemanticMemberIdentity("event_card_set", "events", 1, SCOPE, HASH)


def _oracle(value):
    # All keys here are ASCII, so this independent sorted-key encoding is JCS.
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _member(**changes):
    return replace(ArtifactMember("event_card_set", "events", 1, SCOPE, HASH, PAYLOAD), **changes)


def _committed(**changes):
    return replace(
        CommittedArtifactMemberReference(
            UUID(int=1), UUID(int=2), 1, SCOPE, "event_card_set", "events", 1, HASH
        ),
        **changes,
    )


def test_exact_wire_roundtrip_and_independent_hash_oracle():
    wire = {
        "artifact_type": "event_card_set",
        "logical_id": "events",
        "revision": 1,
        "scope": {"namespace": "pipeline", "kind": "job", "key": "semantic-unit"},
        "content_hash": HASH,
    }
    assert IDENTITY.to_mapping() == wire
    assert SemanticMemberIdentity.from_mapping(wire) == IDENTITY
    assert IDENTITY.canonical_hash == _oracle(wire)
    obj = SemanticObjectRef(IDENTITY, "event", "事件一")
    assert obj.to_mapping() == {"member_ref": wire, "object_type": "event", "object_id": "事件一"}
    assert SemanticObjectRef.from_mapping(obj.to_mapping()) == obj
    assert obj.canonical_hash == _oracle(obj.to_mapping())
    assert (
        SemanticMemberIdentity.from_mapping(dict(reversed(list(wire.items())))).canonical_hash
        == IDENTITY.canonical_hash
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_type", "narrative_graph"),
        ("logical_id", "other"),
        ("revision", 2),
        ("content_hash", "sha256:" + "b" * 64),
    ],
)
def test_every_member_identity_dimension_changes_hash(field, value):
    other = replace(IDENTITY, **{field: value})
    assert other != IDENTITY and other.canonical_hash != IDENTITY.canonical_hash
    assert SemanticObjectRef(other, "event", "same-id") != SemanticObjectRef(
        IDENTITY, "event", "same-id"
    )


@pytest.mark.parametrize("field", ["namespace", "kind", "key"])
def test_every_scope_dimension_changes_owner_identity(field):
    other = replace(IDENTITY, scope=replace(SCOPE, **{field: "other"}))
    assert other != IDENTITY and other.canonical_hash != IDENTITY.canonical_hash
    assert (
        SemanticObjectRef(other, "event", "same-id").canonical_hash
        != SemanticObjectRef(IDENTITY, "event", "same-id").canonical_hash
    )


@pytest.mark.parametrize("field,value", [("object_type", "fact"), ("object_id", "event-two")])
def test_object_type_and_id_are_identity_dimensions(field, value):
    obj = SemanticObjectRef(IDENTITY, "event", "event-one")
    other = replace(obj, **{field: value})
    assert other != obj and other.canonical_hash != obj.canonical_hash


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, 1.5, "1", None, 2**53])
def test_revision_rejects_wrong_types_nonpositive_and_unsafe_integer(value):
    with pytest.raises(SemanticReferenceError):
        replace(IDENTITY, revision=value)
    wire = IDENTITY.to_mapping()
    wire["revision"] = value
    with pytest.raises(SemanticReferenceError):
        SemanticMemberIdentity.from_mapping(wire)


def test_maximum_safe_revision_and_unicode_are_not_normalized():
    identity = SemanticMemberIdentity(
        "事实集", "事件-é", 2**53 - 1, ArtifactScope("叙事", "任务", "第一集"), HASH
    )
    assert SemanticMemberIdentity.from_mapping(identity.to_mapping()) == identity
    assert identity.canonical_hash == _oracle(identity.to_mapping())
    assert replace(identity, logical_id="事件-e\u0301").canonical_hash != identity.canonical_hash


@pytest.mark.parametrize("field", ["artifact_type", "logical_id", "content_hash"])
@pytest.mark.parametrize("value", [None, True, 1, 1.0, [], {}, "", " \n ", "\ud800"])
def test_member_text_requires_actual_nonempty_utf8_strings(field, value):
    with pytest.raises(SemanticReferenceError):
        replace(IDENTITY, **{field: value})


@pytest.mark.parametrize("field", ["object_type", "object_id"])
@pytest.mark.parametrize("value", [None, False, 1, 1.0, [], {}, "", "\t", "\udfff"])
def test_object_text_requires_actual_nonempty_utf8_strings(field, value):
    with pytest.raises(SemanticReferenceError):
        replace(SemanticObjectRef(IDENTITY, "event", "one"), **{field: value})


@pytest.mark.parametrize(
    "digest",
    [
        "SHA256:" + "a" * 64,
        "sha256:" + "A" * 64,
        "a" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "g" * 64,
        "sha256:" + "a" * 64 + "\n",
    ],
)
def test_hash_grammar_is_exact_lowercase_sha256(digest):
    with pytest.raises(SemanticReferenceError):
        replace(IDENTITY, content_hash=digest)


@pytest.mark.parametrize("value", [None, {}, SCOPE, IDENTITY.to_mapping()])
def test_object_member_must_be_exact_typed_identity(value):
    with pytest.raises(SemanticReferenceError):
        SemanticObjectRef(value, "event", "one")


@pytest.mark.parametrize("value", [None, {}, "pipeline/job/key", ("pipeline", "job", "key")])
def test_scope_must_be_exact_artifact_scope(value):
    with pytest.raises(SemanticReferenceError):
        replace(IDENTITY, scope=value)


@pytest.mark.parametrize("field", ["namespace", "kind", "key"])
@pytest.mark.parametrize("value", [None, True, 1, "", " ", "\ud800"])
def test_scope_mapping_validates_every_text_field(field, value):
    wire = IDENTITY.to_mapping()
    wire["scope"][field] = value
    with pytest.raises(SemanticReferenceError):
        SemanticMemberIdentity.from_mapping(wire)


def test_direct_scope_with_invalid_utf8_is_rejected_even_if_store_scope_accepts_it():
    with pytest.raises(SemanticReferenceError):
        replace(IDENTITY, scope=ArtifactScope("pipeline", "job", "\ud800"))


@pytest.mark.parametrize("location", ["object", "member", "scope"])
@pytest.mark.parametrize("change", ["extra", "missing", "wrong_container"])
def test_closed_recursive_mapping_rejects_unknown_missing_or_mistyped_fields(location, change):
    wire = SemanticObjectRef(IDENTITY, "event", "one").to_mapping()
    target = {"object": wire, "member": wire["member_ref"], "scope": wire["member_ref"]["scope"]}[
        location
    ]
    if change == "extra":
        target["artifact_id"] = "forbidden-db-id"
    elif change == "missing":
        del target[next(iter(target))]
    elif location == "object":
        wire = []
    elif location == "member":
        wire["member_ref"] = IDENTITY
    else:
        wire["member_ref"]["scope"] = SCOPE
    with pytest.raises(SemanticReferenceError):
        SemanticObjectRef.from_mapping(wire)


def test_actual_types_reject_subclasses():
    class Text(str):
        pass

    class Number(int):
        pass

    class Scope(ArtifactScope):
        pass

    class Identity(SemanticMemberIdentity):
        pass

    for field, value in (
        ("artifact_type", Text("events")),
        ("revision", Number(1)),
        ("scope", Scope("pipeline", "job", "key")),
    ):
        with pytest.raises(SemanticReferenceError):
            replace(IDENTITY, **{field: value})
    with pytest.raises(SemanticReferenceError):
        SemanticObjectRef(Identity("events", "events", 1, SCOPE, HASH), "event", "one")
    wire = IDENTITY.to_mapping()
    wire[Text("revision")] = wire.pop("revision")
    with pytest.raises(SemanticReferenceError):
        SemanticMemberIdentity.from_mapping(wire)


def test_nested_values_frozen_and_every_mapping_fresh():
    obj = SemanticObjectRef(IDENTITY, "event", "one")
    for value, field, replacement in (
        (obj, "object_id", "two"),
        (IDENTITY, "revision", 2),
        (SCOPE, "key", "other"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
    first, second = obj.to_mapping(), obj.to_mapping()
    first["member_ref"]["scope"]["key"] = "modified"
    assert obj.to_mapping() == second != first
    original = deepcopy(second)
    decoded = SemanticObjectRef.from_mapping(second)
    second["member_ref"]["logical_id"] = "modified"
    assert decoded.to_mapping() == original


def test_real_member_projection_checks_store_payload_hash_without_changing_its_codec():
    assert SemanticMemberIdentity.from_artifact_member(_member()) == IDENTITY
    assert (
        SemanticMemberIdentity.from_artifact_member(
            _member(payload_json=json.dumps(json.loads(PAYLOAD), indent=2))
        )
        == IDENTITY
    )
    # Payload grammar/hashing stays Store-owned; identity JCS has no float fields.
    float_payload = '{"measurement":0.5}'
    member = _member(payload_json=float_payload, content_hash=canonical_payload_hash(float_payload))
    assert SemanticMemberIdentity.from_artifact_member(member).content_hash == member.content_hash
    with pytest.raises(SemanticReferenceError, match="does not match"):
        SemanticMemberIdentity.from_artifact_member(_member(content_hash="sha256:" + "b" * 64))
    with pytest.raises(SemanticReferenceError):
        SemanticMemberIdentity.from_artifact_member(_member(payload_json='{"bad":NaN}'))


def test_committed_projection_is_only_value_identity_not_receipt_or_set_authority():
    first = SemanticMemberIdentity.from_committed_member_reference(_committed())
    other = SemanticMemberIdentity.from_committed_member_reference(
        _committed(receipt_id=UUID(int=8), artifact_set_id=UUID(int=9), member_ordinal=7)
    )
    assert first == other == IDENTITY
    assert set(first.to_mapping()) == {
        "artifact_type",
        "logical_id",
        "revision",
        "scope",
        "content_hash",
    }
    assert not hasattr(first, "artifact_id") and not hasattr(first, "receipt_id")


@pytest.mark.parametrize("value", [None, {}, IDENTITY, SCOPE])
def test_projection_rejects_non_store_values(value):
    with pytest.raises(SemanticReferenceError):
        SemanticMemberIdentity.from_artifact_member(value)
    with pytest.raises(SemanticReferenceError):
        SemanticMemberIdentity.from_committed_member_reference(value)


@pytest.mark.parametrize("projection", ["artifact", "committed"])
def test_projection_rechecks_safe_revision_beyond_store_shape(projection):
    with pytest.raises(SemanticReferenceError, match="safe integer"):
        if projection == "artifact":
            SemanticMemberIdentity.from_artifact_member(_member(revision=2**53))
        else:
            SemanticMemberIdentity.from_committed_member_reference(_committed(revision=2**53))
