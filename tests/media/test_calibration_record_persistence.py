from __future__ import annotations

import json
from dataclasses import replace

import autocut_kernel.media as media
import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.media import (
    CALIBRATION_BOUND_ALGORITHM_SHA256,
    CALIBRATION_RECORD_ARTIFACT_TYPE,
    CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE,
    CALIBRATION_VALIDATION_RECEIPT_ARTIFACT_TYPE,
    CalibrationEvidenceMember,
    CalibrationMatchEvidence,
    CalibrationRecordCandidate,
    CalibrationRecordError,
    CalibrationRecordIdentity,
    CalibrationRecordMemberPayload,
    CalibrationRecordPayload,
    CalibrationRecordProducerIdentity,
    CalibrationRecordRole,
    TickRange,
    TimeBase,
    build_calibration_record_candidate,
    decode_calibration_record_member_payload,
    decode_calibration_record_payload,
    verify_calibration_record_candidate,
)
from autocut_kernel.media.calibration_record import (
    CALIBRATION_VALIDATION_CHECKS,
    CALIBRATION_VALIDATOR_COMMAND,
    CALIBRATION_VALIDATOR_PRINCIPAL,
    CalibrationRecordArtifactMember,
    CalibrationRecordArtifactSet,
    CalibrationRecordScope,
    CalibrationValidationReceiptPayload,
    IndependentlyRecomputedCalibrationResult,
    calibration_validation_input_hash,
    calibration_validation_result_hash,
    decode_calibration_validation_receipt_payload,
    validator_internal_assemble_accepted_artifact_set,
    verify_calibration_record_artifact_set,
)


def _sha(number: int) -> str:
    return f"sha256:{number:064x}"


def _identity() -> CalibrationRecordIdentity:
    return CalibrationRecordIdentity(
        profile_source_sha256=_sha(1),
        registry_snapshot_sha256=_sha(2),
        calibration_corpus_set_sha256=_sha(3),
        native_port_identity_sha256=_sha(4),
        source_clock_id="source-audio-clock",
        source_time_base=TimeBase(1, 1_000),
        timed_speech_policy_sha256=_sha(5),
        word_gap_policy_sha256=_sha(6),
        vad_merge_policy_sha256=_sha(7),
        alignment_policy_sha256=_sha(8),
        acceptance_policy_sha256=_sha(9),
    )


def _producer(role: CalibrationRecordRole) -> CalibrationRecordProducerIdentity:
    is_asr = role is CalibrationRecordRole.ASR
    return CalibrationRecordProducerIdentity(
        role=role,
        producer_id="sensevoice-asr" if is_asr else "fsmn-vad",
        producer_version="1.0.0",
        generation_policy_sha256=_sha(11 if is_asr else 12),
        detector_sha256=_sha(13 if is_asr else 14),
        calibration_policy_sha256=_sha(15 if is_asr else 16),
        model_id="SenseVoiceSmall" if is_asr else "fsmn-vad",
        model_revision="main",
        model_sha256=_sha(17 if is_asr else 18),
        inference_kind="sensevoice-word-timestamp" if is_asr else "fsmn-vad-direct",
        service_sha256=_sha(19),
    )


def _evidence() -> tuple[CalibrationEvidenceMember, ...]:
    return (
        CalibrationEvidenceMember(0, _sha(20), _sha(21), _sha(22), _sha(23)),
        CalibrationEvidenceMember(1, _sha(24), _sha(25), _sha(26), _sha(27)),
    )


def _child(
    role: CalibrationRecordRole,
    *,
    identity: CalibrationRecordIdentity | None = None,
    producer: CalibrationRecordProducerIdentity | None = None,
) -> CalibrationRecordMemberPayload:
    if role is CalibrationRecordRole.ASR:
        matches = (
            CalibrationMatchEvidence.from_ranges(
                "asr-anchor-0", "asr-observation-0", TickRange(100, 200), TickRange(98, 203)
            ),
            CalibrationMatchEvidence.from_ranges(
                "asr-anchor-1", "asr-observation-1", TickRange(300, 400), TickRange(301, 402)
            ),
        )
    else:
        matches = (
            CalibrationMatchEvidence.from_ranges(
                "vad-anchor-0", "vad-observation-0", TickRange(500, 700), TickRange(504, 698)
            ),
        )
    return CalibrationRecordMemberPayload.from_matches(
        identity or _identity(), producer or _producer(role), _evidence(), matches
    )


def _record_set() -> CalibrationRecordArtifactSet:
    identity = _identity()
    candidate = build_calibration_record_candidate(
        profile_version="1",
        identity=identity,
        measurement_manifest_sha256=_sha(28),
        measurement_results_sha256=_sha(29),
        asr=_child(CalibrationRecordRole.ASR, identity=identity),
        vad=_child(CalibrationRecordRole.VAD, identity=identity),
    )
    return validator_internal_assemble_accepted_artifact_set(_proof(candidate))


def _proof(candidate: CalibrationRecordCandidate) -> IndependentlyRecomputedCalibrationResult:
    profile_key = f"shadow_calibration@{candidate.profile_version}"
    return IndependentlyRecomputedCalibrationResult(
        candidate,
        CALIBRATION_VALIDATION_CHECKS,
        calibration_validation_input_hash(
            profile_key=profile_key,
            identity=candidate.aggregate.identity,
            measurement_manifest_sha256=candidate.aggregate.measurement_manifest_sha256,
            measurement_results_sha256=candidate.aggregate.measurement_results_sha256,
            asr=candidate.asr,
            vad=candidate.vad,
        ),
        calibration_validation_result_hash(candidate.aggregate, candidate.asr, candidate.vad),
        CALIBRATION_VALIDATOR_COMMAND,
        CALIBRATION_VALIDATOR_PRINCIPAL,
    )


def _artifact(
    original: CalibrationRecordArtifactMember,
    *,
    ordinal: int | None = None,
    artifact_type: str | None = None,
    logical_id: str | None = None,
    scope: CalibrationRecordScope | None = None,
    payload: CalibrationRecordPayload
    | CalibrationRecordMemberPayload
    | CalibrationValidationReceiptPayload
    | None = None,
) -> CalibrationRecordArtifactMember:
    actual_payload = original.payload if payload is None else payload
    return CalibrationRecordArtifactMember(
        original.ordinal if ordinal is None else ordinal,
        original.artifact_type if artifact_type is None else artifact_type,
        original.logical_id if logical_id is None else logical_id,
        original.revision,
        original.scope if scope is None else scope,
        actual_payload.content_hash,
        actual_payload,
    )


def test_builder_emits_exact_four_member_authority_closure_and_distinct_hashes() -> None:
    record_set = _record_set()

    assert tuple(member.ordinal for member in record_set.members) == (0, 1, 2, 3)
    assert tuple(member.artifact_type for member in record_set.members) == (
        CALIBRATION_RECORD_ARTIFACT_TYPE,
        CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE,
        CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE,
        CALIBRATION_VALIDATION_RECEIPT_ARTIFACT_TYPE,
    )
    assert tuple(member.logical_id for member in record_set.members) == (
        "calibration-record/aggregate/shadow_calibration@1/1",
        "calibration-record/member/asr/shadow_calibration@1/1",
        "calibration-record/member/vad/shadow_calibration@1/1",
        "calibration-record/validation/shadow_calibration@1/1",
    )
    assert all(
        member.scope == CalibrationRecordScope("autocut_authority", "calibration", "shadow_calibration@1")
        and member.revision == 1
        for member in record_set.members
    )
    assert len({member.content_hash for member in record_set.members[:3]}) == 3
    assert record_set.aggregate.asr_member_sha256 == record_set.members[1].content_hash
    assert record_set.aggregate.vad_member_sha256 == record_set.members[2].content_hash
    assert record_set.aggregate.asr_accepted_bound_tick == 3
    assert record_set.aggregate.vad_accepted_bound_tick == 4


def test_public_media_facade_can_build_only_an_unaccepted_candidate() -> None:
    identity = _identity()
    candidate = media.build_calibration_record_candidate(
        profile_version="1",
        identity=identity,
        measurement_manifest_sha256=_sha(28),
        measurement_results_sha256=_sha(29),
        asr=_child(CalibrationRecordRole.ASR, identity=identity),
        vad=_child(CalibrationRecordRole.VAD, identity=identity),
    )

    assert isinstance(candidate, CalibrationRecordCandidate)
    verify_calibration_record_candidate(candidate)
    assert not hasattr(candidate, "validation")
    assert not hasattr(candidate, "members")
    for forbidden_name in (
        "CalibrationRecordArtifactMember",
        "CalibrationRecordArtifactSet",
        "CalibrationValidationReceiptPayload",
        "IndependentlyRecomputedCalibrationResult",
        "build_calibration_record_artifact_set",
        "decode_calibration_validation_receipt_payload",
        "verify_calibration_record_artifact_set",
        "validator_internal_assemble_accepted_artifact_set",
    ):
        assert not hasattr(media, forbidden_name)


def test_canonical_bytes_hashes_and_decoders_are_deterministic() -> None:
    first, second = _record_set(), _record_set()

    assert tuple(item.payload_bytes for item in first.members) == tuple(
        item.payload_bytes for item in second.members
    )
    assert tuple(item.content_hash for item in first.members) == tuple(
        item.content_hash for item in second.members
    )
    assert decode_calibration_record_payload(first.members[0].payload_bytes) == first.aggregate
    assert decode_calibration_record_member_payload(first.members[1].payload_bytes) == first.asr
    assert decode_calibration_record_member_payload(first.members[2].payload_bytes) == first.vad
    assert decode_calibration_validation_receipt_payload(first.members[3].payload_bytes) == first.validation


def test_bound_algorithm_identity_is_module_owned_and_substitution_is_rejected() -> None:
    record = _record_set()
    assert record.aggregate.identity.bound_algorithm_sha256 == CALIBRATION_BOUND_ALGORITHM_SHA256
    assert record.validation.bound_algorithm_sha256 == CALIBRATION_BOUND_ALGORITHM_SHA256
    assert "registry_snapshot_sha256" in record.aggregate.identity.to_mapping()
    assert "registry_set_sha256" not in record.aggregate.identity.to_mapping()

    aggregate = record.aggregate.to_mapping()
    identity = aggregate["identity"]
    assert isinstance(identity, dict)
    identity["bound_algorithm_sha256"] = _sha(99)
    with pytest.raises(CalibrationRecordError, match="frozen algorithm"):
        decode_calibration_record_payload(canonical_json_bytes(aggregate))
    with pytest.raises(CalibrationRecordError, match="module-owned algorithm"):
        replace(record.validation, bound_algorithm_sha256=_sha(99))


@pytest.mark.parametrize(
    "decoder,payload",
    [
        (decode_calibration_record_payload, lambda record: record.members[0].payload_bytes),
        (decode_calibration_record_member_payload, lambda record: record.members[1].payload_bytes),
        (
            decode_calibration_validation_receipt_payload,
            lambda record: record.members[3].payload_bytes,
        ),
    ],
)
def test_byte_decoders_reject_duplicate_keys_and_floats(decoder: object, payload: object) -> None:
    decode = decoder
    get_payload = payload
    assert callable(decode) and callable(get_payload)
    raw = get_payload(_record_set())
    assert isinstance(raw, bytes)
    duplicate = raw[:-1] + b',"schema_version":"forged"}'
    with pytest.raises(CalibrationRecordError, match="strict UTF-8"):
        decode(duplicate)
    with pytest.raises(CalibrationRecordError, match="strict UTF-8"):
        decode(raw.replace(b'"schema_version"', b'"injected_float":1.5,"schema_version"', 1))


@pytest.mark.parametrize(
    "decoder,ordinal",
    [
        (decode_calibration_record_payload, 0),
        (decode_calibration_record_member_payload, 1),
        (decode_calibration_validation_receipt_payload, 3),
    ],
)
@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: b" " + raw,
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b"calibration-record", b"calibration\\u002drecord", 1),
        lambda raw: json.dumps(
            dict(reversed(list(json.loads(raw).items()))),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode(),
    ],
    ids=("leading-whitespace", "trailing-newline", "escaped-character", "wrong-key-order"),
)
def test_decoders_require_exact_canonical_bytes(
    mutate: object, decoder: object, ordinal: int
) -> None:
    transform = mutate
    decode = decoder
    assert callable(transform) and callable(decode)
    raw = _record_set().members[ordinal].payload_bytes
    changed = transform(raw)
    assert isinstance(changed, bytes) and changed != raw
    with pytest.raises(CalibrationRecordError, match="exact canonical encoding"):
        decode(changed)


@pytest.mark.parametrize("bad_bound", [0, -1, True, 1.5])
def test_decoders_and_types_reject_nonpositive_or_noninteger_bounds(bad_bound: object) -> None:
    mapping = _record_set().aggregate.to_mapping()
    mapping["asr_accepted_bound_tick"] = bad_bound
    raw = json.dumps(mapping, separators=(",", ":"), sort_keys=True).encode()
    with pytest.raises(CalibrationRecordError):
        decode_calibration_record_payload(raw)


def test_decoders_reject_unknown_nested_keys_and_zero_hashes() -> None:
    record = _record_set()
    aggregate = record.aggregate.to_mapping()
    aggregate["unknown"] = "forged"
    with pytest.raises(CalibrationRecordError, match="closed schema"):
        decode_calibration_record_payload(canonical_json_bytes(aggregate))

    child = record.asr.to_mapping()
    identity = child["identity"]
    assert isinstance(identity, dict)
    identity["profile_source_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(CalibrationRecordError, match="non-zero"):
        decode_calibration_record_member_payload(canonical_json_bytes(child))


@pytest.mark.parametrize(
    "namespace,kind,key",
    [
        ("pipeline", "calibration", "shadow_calibration@1"),
        ("autocut_authority", "calibration_records", "shadow_calibration@1"),
        ("autocut_authority", "calibration", "shadow_calibration_v1"),
        ("autocut_authority", "calibration", "shadow_calibration@0"),
    ],
)
def test_wrong_scope_is_rejected(namespace: str, kind: str, key: str) -> None:
    with pytest.raises(CalibrationRecordError):
        CalibrationRecordScope(namespace, kind, key)


def test_wrong_member_order_type_revision_and_logical_id_are_rejected() -> None:
    record = _record_set()
    with pytest.raises(CalibrationRecordError, match="order"):
        verify_calibration_record_artifact_set(
            (record.members[1], record.members[0], record.members[2], record.members[3])
        )
    wrong_type = _artifact(record.members[3], artifact_type="command_receipt")
    with pytest.raises(CalibrationRecordError, match="types"):
        verify_calibration_record_artifact_set((*record.members[:3], wrong_type))
    wrong_id = _artifact(record.members[2], logical_id="calibration-record/member/asr/shadow_calibration@1/1")
    with pytest.raises(CalibrationRecordError, match="logical IDs"):
        verify_calibration_record_artifact_set(
            (record.members[0], record.members[1], wrong_id, record.members[3])
        )
    with pytest.raises(CalibrationRecordError, match="revision"):
        replace(record.members[0], revision=2)


def test_mixed_profile_scope_and_profile_source_registry_policy_drift_are_rejected() -> None:
    record = _record_set()
    other_scope = CalibrationRecordScope.for_profile("2")
    mixed_scope = _artifact(record.members[2], scope=other_scope)
    with pytest.raises(CalibrationRecordError, match="scopes"):
        verify_calibration_record_artifact_set(
            (record.members[0], record.members[1], mixed_scope, record.members[3])
        )

    for field_name in (
        "profile_source_sha256",
        "registry_snapshot_sha256",
        "calibration_corpus_set_sha256",
        "word_gap_policy_sha256",
    ):
        identity = replace(record.vad.identity, **{field_name: _sha(90)})
        changed = _child(CalibrationRecordRole.VAD, identity=identity)
        with pytest.raises(CalibrationRecordError, match="identit"):
            build_calibration_record_candidate(
                profile_version="1",
                identity=record.aggregate.identity,
                measurement_manifest_sha256=_sha(28),
                measurement_results_sha256=_sha(29),
                asr=record.asr,
                vad=changed,
            )


@pytest.mark.parametrize("field_name", ["producer_id", "detector_sha256", "model_sha256"])
def test_asr_vad_producer_identity_fields_are_distinct(field_name: str) -> None:
    record = _record_set()
    same_value = replace(
        record.vad.producer_identity,
        **{field_name: getattr(record.asr.producer_identity, field_name)},
    )
    changed_vad = _child(CalibrationRecordRole.VAD, producer=same_value)
    with pytest.raises(CalibrationRecordError, match=field_name):
        build_calibration_record_candidate(
            profile_version="1",
            identity=record.aggregate.identity,
            measurement_manifest_sha256=_sha(28),
            measurement_results_sha256=_sha(29),
            asr=record.asr,
            vad=changed_vad,
        )


def test_producer_roles_inference_kinds_and_exact_model_ids_are_frozen() -> None:
    record = _record_set()
    assert record.asr.producer_identity.model_id == "SenseVoiceSmall"
    assert record.vad.producer_identity.model_id == "fsmn-vad"
    assert record.asr.to_mapping()["producer_kind"] == "asr"
    producer_mapping = record.asr.to_mapping()["producer_identity"]
    assert isinstance(producer_mapping, dict)
    assert producer_mapping["producer_kind"] == "asr"
    assert "producer" not in record.asr.to_mapping() and "role" not in producer_mapping
    for producer, mutation in (
        (record.asr.producer_identity, {"inference_kind": "fsmn-vad-direct"}),
        (record.vad.producer_identity, {"inference_kind": "sensevoice-word-timestamp"}),
        (record.asr.producer_identity, {"model_id": "fsmn-vad"}),
        (record.vad.producer_identity, {"model_id": "SenseVoiceSmall"}),
        (
            record.asr.producer_identity,
            {"role": CalibrationRecordRole.VAD, "inference_kind": "fsmn-vad-direct"},
        ),
    ):
        with pytest.raises(CalibrationRecordError):
            replace(producer, **mutation)


def test_equal_child_hashes_are_rejected() -> None:
    record = _record_set()
    with pytest.raises(CalibrationRecordError, match="child hashes"):
        replace(record.aggregate, vad_member_sha256=record.aggregate.asr_member_sha256)


def test_match_errors_and_maxima_are_recomputed_not_caller_claimed() -> None:
    record = _record_set()
    with pytest.raises(CalibrationRecordError, match="non-empty tuple"):
        CalibrationRecordMemberPayload.from_matches(
            _identity(), _producer(CalibrationRecordRole.ASR), _evidence(), ()
        )
    with pytest.raises(CalibrationRecordError, match="endpoint recomputation"):
        replace(record.asr.matches[0], early_tick=99)
    with pytest.raises(CalibrationRecordError, match="canonical match maxima"):
        replace(record.asr, absolute_maximum_tick=99, accepted_bound_tick=99)
    exact = CalibrationMatchEvidence.from_ranges(
        "exact-anchor", "exact-observation", TickRange(1, 2), TickRange(1, 2)
    )
    with pytest.raises(CalibrationRecordError, match="at least 1"):
        CalibrationRecordMemberPayload.from_matches(
            _identity(), _producer(CalibrationRecordRole.ASR), _evidence(), (exact,)
        )


@pytest.mark.parametrize("field_name", ["expected_range", "observed_range"])
@pytest.mark.parametrize("negative_range", [TickRange(-1, 1), TickRange(-2, -1)])
def test_match_construction_rejects_negative_source_clock_ranges(
    field_name: str, negative_range: TickRange
) -> None:
    match = _record_set().asr.matches[0]
    with pytest.raises(CalibrationRecordError, match=f"{field_name}.in_tick must be at least 0"):
        replace(match, **{field_name: negative_range})


def test_nonnegative_match_construction_round_trips_at_source_clock_boundary() -> None:
    intervals = tuple(TickRange(start, end) for start in range(4) for end in range(start + 1, 5))
    for expected in intervals:
        for observed in intervals:
            if expected == observed:
                continue  # A zero-bound child is deliberately not eligible for persistence.
            match = CalibrationMatchEvidence.from_ranges(
                "boundary-anchor", "boundary-observation", expected, observed
            )
            child = CalibrationRecordMemberPayload.from_matches(
                _identity(), _producer(CalibrationRecordRole.ASR), _evidence(), (match,)
            )
            assert decode_calibration_record_member_payload(
                canonical_json_bytes(child.to_mapping())
            ) == child


def test_children_require_the_same_complete_ordered_corpus() -> None:
    record = _record_set()
    with pytest.raises(CalibrationRecordError, match="ordinal order"):
        replace(record.vad, evidence_members=tuple(reversed(record.vad.evidence_members)))
    different = replace(record.vad.evidence_members[1], raw_response_blob_sha256=_sha(91))
    changed_vad = replace(record.vad, evidence_members=(record.vad.evidence_members[0], different))
    aggregate = replace(record.aggregate, vad_member_sha256=changed_vad.content_hash)
    changed_members = (
        _artifact(record.members[0], payload=aggregate),
        record.members[1],
        _artifact(record.members[2], payload=changed_vad),
        record.members[3],
    )
    with pytest.raises(CalibrationRecordError, match="same ordered corpus"):
        verify_calibration_record_artifact_set(changed_members)


@pytest.mark.parametrize(
    "field_name",
    [
        "corpus_member_reference_sha256",
        "expected_anchor_reference_sha256",
        "raw_response_blob_sha256",
        "projection_sha256",
    ],
)
def test_each_evidence_identity_is_individually_unique(field_name: str) -> None:
    child = _record_set().asr
    duplicate = replace(
        child.evidence_members[1],
        **{field_name: getattr(child.evidence_members[0], field_name)},
    )
    with pytest.raises(CalibrationRecordError, match=field_name):
        replace(child, evidence_members=(child.evidence_members[0], duplicate))


def test_anchor_and_observation_ids_are_individually_one_to_one() -> None:
    child = _record_set().asr
    duplicate_anchor = replace(child.matches[1], anchor_id=child.matches[0].anchor_id)
    with pytest.raises(CalibrationRecordError, match="duplicate anchor_id"):
        replace(child, matches=(child.matches[0], duplicate_anchor))
    duplicate_observation = replace(
        child.matches[1], observation_id=child.matches[0].observation_id
    )
    with pytest.raises(CalibrationRecordError, match="duplicate observation_id"):
        replace(child, matches=(child.matches[0], duplicate_observation))


def test_validation_member_cannot_self_assert_acceptance_without_recomputed_bindings() -> None:
    record = _record_set()
    identity = _identity()
    candidate = build_calibration_record_candidate(
        profile_version="1",
        identity=identity,
        measurement_manifest_sha256=_sha(28),
        measurement_results_sha256=_sha(29),
        asr=_child(CalibrationRecordRole.ASR, identity=identity),
        vad=_child(CalibrationRecordRole.VAD, identity=identity),
    )
    with pytest.raises(CalibrationRecordError, match="input proof"):
        IndependentlyRecomputedCalibrationResult(
            candidate,
            CALIBRATION_VALIDATION_CHECKS,
            _sha(91),
            _sha(92),
            CALIBRATION_VALIDATOR_COMMAND,
            CALIBRATION_VALIDATOR_PRINCIPAL,
        )
    with pytest.raises(CalibrationRecordError, match="independently recomputed result"):
        validator_internal_assemble_accepted_artifact_set(candidate)  # type: ignore[arg-type]
    proof = _proof(candidate)
    for changes in (
        {"validation_result_sha256": _sha(93)},
        {"checks": ("positive_bounds",)},
        {"validator_command": "caller-command"},
        {"validator_principal": "caller"},
    ):
        with pytest.raises(CalibrationRecordError):
            replace(proof, **changes)

    forged_receipt = replace(record.validation, validation_input_sha256=_sha(92))
    forged_member = _artifact(record.members[3], payload=forged_receipt)

    decoded_forgery = decode_calibration_validation_receipt_payload(forged_member.payload_bytes)
    assert decoded_forgery.validation_input_sha256 == _sha(92)
    with pytest.raises(CalibrationRecordError, match="recomputed input/result"):
        verify_calibration_record_artifact_set((*record.members[:3], forged_member))


def test_aggregate_or_receipt_substitution_cannot_preserve_set_acceptance() -> None:
    record = _record_set()
    changed_aggregate = replace(record.aggregate, measurement_results_sha256=_sha(93))
    changed_aggregate_member = _artifact(record.members[0], payload=changed_aggregate)
    with pytest.raises(CalibrationRecordError, match="receipt"):
        verify_calibration_record_artifact_set(
            (changed_aggregate_member, *record.members[1:])
        )


def test_artifact_payload_hash_is_always_recomputed() -> None:
    record = _record_set()
    with pytest.raises(CalibrationRecordError, match="canonical payload"):
        replace(record.members[0], content_hash=_sha(99))
