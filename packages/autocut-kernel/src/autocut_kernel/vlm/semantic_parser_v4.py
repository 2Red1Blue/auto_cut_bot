"""Strict v4 provider parsing and context-verified persisted decoding.

The raw-response hash is computed only from actual provider bytes. Persisted
decoding verifies structure and derived identities, not the truth of that hash:
a Store must still reparse its claimed raw Blob and compare the complete pack.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import cast

from ..media.types import sha256_prefixed
from .models import (
    VlmCandidateKind,
    VlmCandidateTag,
    VlmEditingMode,
    VlmEntityKind,
    VlmEventKind,
    VlmFactKind,
    VlmMeasurementKind,
    VlmNarrativeFunction,
    VlmParsePolicy,
    VlmRequestIdentity,
    VlmSemanticMeasurement,
    VlmTemporalMode,
    VlmValidationError,
    VlmWindowSummary,
    derive_vlm_global_id,
)
from .parser import (
    VlmResponseIndeterminate,
    VlmResponseRejected,
    # These pure helpers are deliberately shared from the frozen v3 bundle;
    # no v3 support, entity, event or pack is constructed by this parser.
    _array,  # pyright: ignore[reportPrivateUsage]
    _bool,  # pyright: ignore[reportPrivateUsage]
    _canonical_enums,  # pyright: ignore[reportPrivateUsage]
    _check_count,  # pyright: ignore[reportPrivateUsage]
    _closed,  # pyright: ignore[reportPrivateUsage]
    _constant,  # pyright: ignore[reportPrivateUsage]
    _decimal,  # pyright: ignore[reportPrivateUsage]
    _enum,  # pyright: ignore[reportPrivateUsage]
    _local_id,  # pyright: ignore[reportPrivateUsage]
    _local_ids,  # pyright: ignore[reportPrivateUsage]
    _pairs_object,  # pyright: ignore[reportPrivateUsage]
    _refs,  # pyright: ignore[reportPrivateUsage]
    _reject,  # pyright: ignore[reportPrivateUsage]
    _TextBudget,  # pyright: ignore[reportPrivateUsage]
)
from .semantic_pack_v4 import (
    VlmCandidateHypothesisV4,
    VlmContinuityV4,
    VlmEntityV4,
    VlmEventV4,
    VlmFactV4,
    VlmSemanticPackV4,
    VlmTemporalSegmentV4,
)
from .semantic_support_v4 import decode_support_v4, parse_support_v4
from .window import WindowManifest, WindowManifestSet

VLM_PARSER_STRATEGY_VERSION_V4 = "strict-semantic-pack-v4"


def _validate_context(
    manifest: WindowManifest, manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity, policy: VlmParsePolicy,
) -> None:
    if type(manifest) is not WindowManifest or type(manifest_set) is not WindowManifestSet:  # noqa: E721
        raise VlmValidationError("parser requires exact WindowManifest values")
    if type(request_identity) is not VlmRequestIdentity:  # noqa: E721
        raise VlmValidationError("request_identity must be a VlmRequestIdentity")
    if type(policy) is not VlmParsePolicy:  # noqa: E721
        raise VlmValidationError("policy must be a VlmParsePolicy")
    request_identity.assert_manifest_binding(manifest, manifest_set)
    if request_identity.parse_policy_sha256 != policy.canonical_hash:
        raise VlmValidationError("request identity does not bind the supplied parse policy")


def parse_vlm_response_v4(
    raw_response: bytes,
    *,
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity,
    policy: VlmParsePolicy,
) -> VlmSemanticPackV4:
    """Parse exact provider bytes and derive the complete persisted v4 observation pack."""

    if type(raw_response) is not bytes:  # noqa: E721
        _reject("INVALID_RAW_RESPONSE", "raw_response must be exact bytes")
    _validate_context(manifest, manifest_set, request_identity, policy)
    try:
        return _parse_provider_response(
            raw_response,
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=request_identity,
            policy=policy,
        )
    except VlmValidationError as error:
        raise VlmResponseRejected(
            "SEMANTIC_PACK_INVARIANT_VIOLATION",
            str(error),
        ) from error
    except (RecursionError, UnicodeEncodeError) as error:
        raise VlmResponseRejected("INVALID_JSON", "response contains invalid or excessively nested text") from error


def _parse_provider_response(
    raw_response: bytes,
    *,
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity,
    policy: VlmParsePolicy,
) -> VlmSemanticPackV4:
    """Decode all provider-controlled values behind one rejection boundary."""

    if len(raw_response) > policy.max_response_bytes:
        raise VlmResponseIndeterminate(
            "RESPONSE_BUDGET_EXCEEDED", "raw response exceeds the frozen byte budget"
        )
    try:
        payload = cast(
            object,
            json.loads(
                raw_response.decode("utf-8", "strict"),
                object_pairs_hook=_pairs_object,
                parse_float=Decimal,
                parse_int=int,
                parse_constant=_constant,
            ),
        )
    except UnicodeDecodeError as error:
        raise VlmResponseRejected("INVALID_JSON", "response is not valid UTF-8") from error
    except VlmResponseRejected:
        raise
    except json.JSONDecodeError as error:
        raise VlmResponseRejected("INVALID_JSON", "response is not strict JSON") from error
    except ValueError as error:
        raise VlmResponseRejected("INVALID_JSON", "response contains an unsupported JSON number") from error
    _validate_json_value(payload)
    return _parse_value(
        payload, manifest=manifest, manifest_set=manifest_set,
        request_identity=request_identity, policy=policy,
        raw_response_sha256="sha256:" + hashlib.sha256(raw_response).hexdigest(),
    )


def _parse_value(
    value: object, *, manifest: WindowManifest, manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity, policy: VlmParsePolicy,
    raw_response_sha256: str,
) -> VlmSemanticPackV4:
    root = _closed(
        value,
        frozenset(
            {
                "schema_version",
                "window_summary",
                "continuity",
                "entities",
                "facts",
                "events",
                "candidate_hypotheses",
            }
        ),
        "response",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 4:  # noqa: E721
        _reject("UNSUPPORTED_SCHEMA_VERSION", "schema_version must be integer 4")
    budget = _TextBudget(policy)
    request_hash = request_identity.canonical_hash

    raw_entities = _array(root["entities"], "entities")
    raw_facts = _array(root["facts"], "facts")
    raw_events = _array(root["events"], "events")
    raw_candidates = _array(root["candidate_hypotheses"], "candidate_hypotheses")
    _check_count(raw_entities, policy.max_entities, "entities")
    _check_count(raw_facts, policy.max_facts, "facts")
    _check_count(raw_events, policy.max_events, "events")
    _check_count(raw_candidates, policy.max_candidate_hypotheses, "candidate_hypotheses")
    if not raw_facts:
        _reject("EMPTY_FACTS", "facts must contain at least one visible fact")

    entity_locals = _local_ids(raw_entities, "entities", "local_entity_id")
    fact_locals = _local_ids(raw_facts, "facts", "local_fact_id")
    event_locals = _local_ids(raw_events, "events", "local_event_id")
    _local_ids(raw_candidates, "candidate_hypotheses", "local_candidate_id")
    entity_ids = {
        local_id: derive_vlm_global_id("entity", local_id, request_hash)
        for local_id in entity_locals
    }
    fact_ids = {
        local_id: derive_vlm_global_id("fact", local_id, request_hash) for local_id in fact_locals
    }
    event_ids = {
        local_id: derive_vlm_global_id("event", local_id, request_hash) for local_id in event_locals
    }

    entities: list[VlmEntityV4] = []
    for position, value in enumerate(raw_entities):
        field = f"entities[{position}]"
        item = _closed(
            value,
            frozenset(
                {
                    "local_entity_id",
                    "entity_kind",
                    "display_label",
                    "visual_description",
                    "support",
                }
            ),
            field,
        )
        local_id = _local_id(item["local_entity_id"], f"{field}.local_entity_id")
        entities.append(
            VlmEntityV4(
                entity_id=entity_ids[local_id],
                local_entity_id=local_id,
                entity_kind=_enum(item["entity_kind"], VlmEntityKind, f"{field}.entity_kind"),
                display_label=cast(
                    str, budget.text(item["display_label"], f"{field}.display_label")
                ),
                visual_description=cast(
                    str,
                    budget.text(item["visual_description"], f"{field}.visual_description"),
                ),
                support=parse_support_v4(item["support"], manifest, manifest_set),
            )
        )

    facts: list[VlmFactV4] = []
    for position, value in enumerate(raw_facts):
        field = f"facts[{position}]"
        item = _closed(
            value,
            frozenset(
                {
                    "local_fact_id",
                    "fact_kind",
                    "subject_ref",
                    "object_ref",
                    "summary",
                    "support",
                }
            ),
            field,
        )
        local_id = _local_id(item["local_fact_id"], f"{field}.local_fact_id")
        subject_local = _local_id(item["subject_ref"], f"{field}.subject_ref")
        if subject_local not in entity_ids:
            _reject("UNKNOWN_REFERENCE", f"{field}.subject_ref is not closed")
        object_value = item["object_ref"]
        object_ref: str | None = None
        if object_value is not None:
            object_local = _local_id(object_value, f"{field}.object_ref")
            if object_local not in entity_ids:
                _reject("UNKNOWN_REFERENCE", f"{field}.object_ref is not closed")
            object_ref = entity_ids[object_local]
        facts.append(
            VlmFactV4(
                fact_id=fact_ids[local_id],
                local_fact_id=local_id,
                fact_kind=_enum(item["fact_kind"], VlmFactKind, f"{field}.fact_kind"),
                subject_ref=entity_ids[subject_local],
                object_ref=object_ref,
                summary=cast(str, budget.text(item["summary"], f"{field}.summary")),
                support=parse_support_v4(item["support"], manifest, manifest_set),
            )
        )

    events: list[VlmEventV4] = []
    for position, value in enumerate(raw_events):
        field = f"events[{position}]"
        item = _closed(
            value,
            frozenset(
                {
                    "local_event_id",
                    "event_kind",
                    "summary",
                    "participant_refs",
                    "fact_refs",
                    "cause_event_refs",
                    "effect_event_refs",
                    "open_question",
                    "temporal_mode",
                    "support",
                }
            ),
            field,
        )
        local_id = _local_id(item["local_event_id"], f"{field}.local_event_id")
        events.append(
            VlmEventV4(
                event_id=event_ids[local_id],
                local_event_id=local_id,
                event_kind=_enum(item["event_kind"], VlmEventKind, f"{field}.event_kind"),
                summary=cast(str, budget.text(item["summary"], f"{field}.summary")),
                participant_refs=_refs(
                    item["participant_refs"], f"{field}.participant_refs", entity_ids
                ),
                fact_refs=_refs(item["fact_refs"], f"{field}.fact_refs", fact_ids, nonempty=True),
                cause_event_refs=_refs(
                    item["cause_event_refs"], f"{field}.cause_event_refs", event_ids
                ),
                effect_event_refs=_refs(
                    item["effect_event_refs"], f"{field}.effect_event_refs", event_ids
                ),
                open_question=budget.text(
                    item["open_question"], f"{field}.open_question", nullable=True
                ),
                temporal_mode=_enum(
                    item["temporal_mode"], VlmTemporalMode, f"{field}.temporal_mode"
                ),
                support=parse_support_v4(item["support"], manifest, manifest_set),
            )
        )

    summary_value = _closed(
        root["window_summary"],
        frozenset({"summary", "dominant_temporal_mode", "fact_refs", "event_refs", "confidence"}),
        "window_summary",
    )
    window_summary = VlmWindowSummary(
        summary=cast(str, budget.text(summary_value["summary"], "window_summary.summary")),
        dominant_temporal_mode=_enum(
            summary_value["dominant_temporal_mode"],
            VlmTemporalMode,
            "window_summary.dominant_temporal_mode",
        ),
        fact_refs=_refs(summary_value["fact_refs"], "window_summary.fact_refs", fact_ids),
        event_refs=_refs(summary_value["event_refs"], "window_summary.event_refs", event_ids),
        confidence=_decimal(summary_value["confidence"], "window_summary.confidence"),
    )

    continuity_value = _closed(
        root["continuity"],
        frozenset(
            {
                "starts_mid_event",
                "ends_mid_event",
                "continues_from_previous",
                "continues_into_next",
                "entry_state_fact_refs",
                "exit_state_fact_refs",
                "temporal_segments",
            }
        ),
        "continuity",
    )
    raw_segments = _array(continuity_value["temporal_segments"], "continuity.temporal_segments")
    _check_count(raw_segments, policy.max_temporal_segments, "continuity.temporal_segments")
    segments: list[VlmTemporalSegmentV4] = []
    for position, value in enumerate(raw_segments):
        field = f"continuity.temporal_segments[{position}]"
        item = _closed(
            value,
            frozenset({"mode", "summary", "support"}),
            field,
        )
        segments.append(
            VlmTemporalSegmentV4(
                mode=_enum(item["mode"], VlmTemporalMode, f"{field}.mode"),
                summary=cast(str, budget.text(item["summary"], f"{field}.summary")),
                support=parse_support_v4(item["support"], manifest, manifest_set),
            )
        )
    continuity = VlmContinuityV4(
        starts_mid_event=_bool(continuity_value["starts_mid_event"], "continuity.starts_mid_event"),
        ends_mid_event=_bool(continuity_value["ends_mid_event"], "continuity.ends_mid_event"),
        continues_from_previous=_bool(
            continuity_value["continues_from_previous"], "continuity.continues_from_previous"
        ),
        continues_into_next=_bool(
            continuity_value["continues_into_next"], "continuity.continues_into_next"
        ),
        entry_state_fact_refs=_refs(
            continuity_value["entry_state_fact_refs"],
            "continuity.entry_state_fact_refs",
            fact_ids,
        ),
        exit_state_fact_refs=_refs(
            continuity_value["exit_state_fact_refs"],
            "continuity.exit_state_fact_refs",
            fact_ids,
        ),
        temporal_segments=tuple(segments),
    )

    candidates: list[VlmCandidateHypothesisV4] = []
    total_measurements = 0
    for position, value in enumerate(raw_candidates):
        field = f"candidate_hypotheses[{position}]"
        item = _closed(
            value,
            frozenset(
                {
                    "local_candidate_id",
                    "candidate_kind",
                    "anchor_event_ref",
                    "supporting_event_refs",
                    "context_event_refs",
                    "payoff_event_refs",
                    "open_question",
                    "reason",
                    "anchor_summary",
                    "payoff_or_open_question",
                    "dialogue_excerpt",
                    "editing_modes",
                    "narrative_functions",
                    "tags",
                    "measurements",
                    "support",
                }
            ),
            field,
        )
        local_id = _local_id(item["local_candidate_id"], f"{field}.local_candidate_id")
        anchor_local = _local_id(item["anchor_event_ref"], f"{field}.anchor_event_ref")
        if anchor_local not in event_ids:
            _reject("UNKNOWN_REFERENCE", f"{field}.anchor_event_ref is not closed")
        candidate_kind = _enum(item["candidate_kind"], VlmCandidateKind, f"{field}.candidate_kind")
        payoff_event_refs = _refs(
            item["payoff_event_refs"], f"{field}.payoff_event_refs", event_ids
        )
        open_question = budget.text(item["open_question"], f"{field}.open_question", nullable=True)
        if candidate_kind is VlmCandidateKind.HOOK:
            if open_question is None or payoff_event_refs:
                _reject(
                    "INVALID_CANDIDATE_KIND_RULE",
                    f"{field} hook requires open_question and empty payoff_event_refs",
                )
        elif not payoff_event_refs:
            _reject(
                "INVALID_CANDIDATE_KIND_RULE",
                f"{field} highlight requires non-empty payoff_event_refs",
            )
        raw_measurements = _array(item["measurements"], f"{field}.measurements")
        if not raw_measurements:
            _reject("EMPTY_MEASUREMENTS", f"{field}.measurements must be non-empty")
        total_measurements += len(raw_measurements)
        if total_measurements > policy.max_measurements:
            raise VlmResponseIndeterminate(
                "STRUCTURE_BUDGET_EXCEEDED", "measurements exceed the frozen item budget"
            )
        measurements: list[VlmSemanticMeasurement] = []
        for measurement_position, measurement_value in enumerate(raw_measurements):
            measurement_field = f"{field}.measurements[{measurement_position}]"
            measurement = _closed(
                measurement_value,
                frozenset({"measurement_kind", "value", "confidence", "fact_refs", "event_refs"}),
                measurement_field,
            )
            fact_refs = _refs(measurement["fact_refs"], f"{measurement_field}.fact_refs", fact_ids)
            measurement_event_refs = _refs(
                measurement["event_refs"], f"{measurement_field}.event_refs", event_ids
            )
            if not fact_refs and not measurement_event_refs:
                _reject(
                    "EMPTY_MEASUREMENT_SUPPORT",
                    f"{measurement_field} refs must be non-empty collectively",
                )
            measurements.append(
                VlmSemanticMeasurement(
                    measurement_kind=_enum(
                        measurement["measurement_kind"],
                        VlmMeasurementKind,
                        f"{measurement_field}.measurement_kind",
                    ),
                    value=_decimal(measurement["value"], f"{measurement_field}.value"),
                    confidence=_decimal(
                        measurement["confidence"], f"{measurement_field}.confidence"
                    ),
                    fact_refs=fact_refs,
                    event_refs=measurement_event_refs,
                )
            )
        candidates.append(
            VlmCandidateHypothesisV4(
                candidate_id=derive_vlm_global_id("candidate", local_id, request_hash),
                local_candidate_id=local_id,
                candidate_kind=candidate_kind,
                anchor_event_ref=event_ids[anchor_local],
                supporting_event_refs=_refs(
                    item["supporting_event_refs"],
                    f"{field}.supporting_event_refs",
                    event_ids,
                ),
                context_event_refs=_refs(
                    item["context_event_refs"], f"{field}.context_event_refs", event_ids
                ),
                payoff_event_refs=payoff_event_refs,
                open_question=open_question,
                reason=cast(str, budget.text(item["reason"], f"{field}.reason")),
                anchor_summary=cast(
                    str, budget.text(item["anchor_summary"], f"{field}.anchor_summary")
                ),
                payoff_or_open_question=cast(
                    str,
                    budget.text(
                        item["payoff_or_open_question"],
                        f"{field}.payoff_or_open_question",
                    ),
                ),
                dialogue_excerpt=budget.text(
                    item["dialogue_excerpt"], f"{field}.dialogue_excerpt", nullable=True
                ),
                editing_modes=_canonical_enums(
                    item["editing_modes"], f"{field}.editing_modes", VlmEditingMode
                ),
                narrative_functions=_canonical_enums(
                    item["narrative_functions"],
                    f"{field}.narrative_functions",
                    VlmNarrativeFunction,
                ),
                tags=_canonical_enums(item["tags"], f"{field}.tags", VlmCandidateTag),
                measurements=tuple(measurements),
                support=parse_support_v4(item["support"], manifest, manifest_set),
            )
        )

    return VlmSemanticPackV4(
        request_identity_sha256=request_hash,
        window_manifest_sha256=manifest.canonical_hash,
        raw_response_sha256=raw_response_sha256,
        window_summary=window_summary,
        continuity=continuity,
        entities=tuple(sorted(entities, key=lambda item: item.local_entity_id)),
        facts=tuple(sorted(facts, key=lambda item: item.local_fact_id)),
        events=tuple(sorted(events, key=lambda item: item.local_event_id)),
        candidate_hypotheses=tuple(
            sorted(candidates, key=lambda item: item.local_candidate_id)
        ),
    )


def _validate_json_value(value: object, depth: int = 0) -> None:
    """Reject non-JSON Python objects, surrogate text and unbounded nesting."""
    if depth > 64:
        raise VlmValidationError("v4 JSON nesting exceeds the bounded structural contract")
    if type(value) is dict:
        for key, member in cast(dict[object, object], value).items():
            if type(key) is not str:  # noqa: E721
                raise VlmValidationError("v4 JSON field names must be strings")
            key.encode("utf-8", "strict")
            _validate_json_value(member, depth + 1)
    elif type(value) is list:
        for member in cast(list[object], value):
            _validate_json_value(member, depth + 1)
    elif type(value) is str:
        value.encode("utf-8", "strict")
    elif value is not None and type(value) not in (int, bool):
        # All semantic decimal values are strings; media times are exact ints.
        raise VlmValidationError("v4 JSON does not accept floating-point or arbitrary object values")


def _local_reference(value: object, identities: dict[str, str], field: str) -> str:
    if type(value) is not str or value not in identities:  # noqa: E721
        raise VlmValidationError(f"{field} must name an exact Kernel-derived global identity")
    return identities[value]


def _local_references(value: object, identities: dict[str, str], field: str) -> list[str]:
    return [_local_reference(item, identities, field) for item in _array(value, field)]


def _persisted_identities(
    values: object, kind: str, request_hash: str,
) -> tuple[list[object], dict[str, str]]:
    items = _array(values, kind)
    local_field, global_field = f"local_{kind}_id", f"{kind}_id"
    locals_ = _local_ids(items, kind, local_field)
    identities = {derive_vlm_global_id(kind, local, request_hash): local for local in locals_}
    for value in items:
        # _local_ids has established exact dict values with the local field.
        item = cast(dict[str, object], value)
        expected = derive_vlm_global_id(kind, cast(str, item[local_field]), request_hash)
        if item.get(global_field) != expected:
            raise VlmValidationError(f"{kind} global ID differs from the exact request-derived ID")
    return items, identities


def _wire_support(value: object, manifest: WindowManifest, manifest_set: WindowManifestSet) -> dict[str, object]:
    """Reverse only a context-verified v4 support, never fabricate frame proof."""
    return decode_support_v4(value, manifest, manifest_set).to_wire_mapping()


def _decode_value(
    value: object, *, manifest: WindowManifest, manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity, policy: VlmParsePolicy,
) -> VlmSemanticPackV4:
    _validate_json_value(value)
    root = _closed(value, frozenset({
        "schema_version", "provenance", "window_summary", "continuity", "entities",
        "facts", "events", "candidate_hypotheses",
    }), "semantic_pack")
    if type(root["schema_version"]) is not int or root["schema_version"] != 4:  # noqa: E721
        raise VlmValidationError("semantic_pack.schema_version must be integer 4")
    provenance = _closed(root["provenance"], frozenset({
        "raw_response_sha256", "request_identity_sha256", "window_manifest_sha256",
    }), "semantic_pack.provenance")
    for name, digest in provenance.items():
        if type(digest) is not str:  # noqa: E721
            raise VlmValidationError(f"provenance.{name} must be an exact SHA-256 string")
        sha256_prefixed(digest, name)
    if (
        provenance["request_identity_sha256"] != request_identity.canonical_hash
        or provenance["window_manifest_sha256"] != manifest.canonical_hash
    ):
        raise VlmValidationError("persisted v4 provenance does not match the supplied exact context")

    for field, maximum in (
        ("entities", policy.max_entities), ("facts", policy.max_facts),
        ("events", policy.max_events), ("candidate_hypotheses", policy.max_candidate_hypotheses),
    ):
        _check_count(_array(root[field], field), maximum, field)
    request_hash = request_identity.canonical_hash
    entities, entity_ids = _persisted_identities(root["entities"], "entity", request_hash)
    facts, fact_ids = _persisted_identities(root["facts"], "fact", request_hash)
    events, event_ids = _persisted_identities(root["events"], "event", request_hash)
    candidates, _ = _persisted_identities(root["candidate_hypotheses"], "candidate", request_hash)

    def member(raw: object, kind: str) -> dict[str, object]:
        # _persisted_identities already checked exact mapping and derived ID.
        item = dict(cast(dict[str, object], raw))
        del item[f"{kind}_id"]
        if "support" not in item:
            raise VlmValidationError(f"{kind}.support is missing")
        item["support"] = _wire_support(item["support"], manifest, manifest_set)
        return item

    wire_entities = [member(item, "entity") for item in entities]
    wire_facts = [member(item, "fact") for item in facts]
    for item in wire_facts:
        item["subject_ref"] = _local_reference(item.get("subject_ref"), entity_ids, "fact.subject_ref")
        if item.get("object_ref") is not None:
            item["object_ref"] = _local_reference(item["object_ref"], entity_ids, "fact.object_ref")
    wire_events = [member(item, "event") for item in events]
    for item in wire_events:
        for field, identities in (
            ("participant_refs", entity_ids), ("fact_refs", fact_ids),
            ("cause_event_refs", event_ids), ("effect_event_refs", event_ids),
        ):
            item[field] = _local_references(item.get(field), identities, f"event.{field}")

    summary = dict(_closed(root["window_summary"], frozenset({
        "summary", "dominant_temporal_mode", "fact_refs", "event_refs", "confidence",
    }), "window_summary"))
    summary["fact_refs"] = _local_references(summary["fact_refs"], fact_ids, "window_summary.fact_refs")
    summary["event_refs"] = _local_references(summary["event_refs"], event_ids, "window_summary.event_refs")
    continuity = dict(_closed(root["continuity"], frozenset({
        "starts_mid_event", "ends_mid_event", "continues_from_previous", "continues_into_next",
        "entry_state_fact_refs", "exit_state_fact_refs", "temporal_segments",
    }), "continuity"))
    for field in ("entry_state_fact_refs", "exit_state_fact_refs"):
        continuity[field] = _local_references(continuity[field], fact_ids, f"continuity.{field}")
    raw_segments = _array(continuity["temporal_segments"], "continuity.temporal_segments")
    _check_count(raw_segments, policy.max_temporal_segments, "continuity.temporal_segments")
    segments: list[dict[str, object]] = []
    for raw in raw_segments:
        item = dict(_closed(raw, frozenset({"mode", "summary", "support"}), "temporal_segment"))
        item["support"] = _wire_support(item["support"], manifest, manifest_set)
        segments.append(item)
    continuity["temporal_segments"] = segments

    wire_candidates = [member(item, "candidate") for item in candidates]
    measurement_count = 0
    for item in wire_candidates:
        item["anchor_event_ref"] = _local_reference(item.get("anchor_event_ref"), event_ids, "candidate.anchor_event_ref")
        for field in ("supporting_event_refs", "context_event_refs", "payoff_event_refs"):
            item[field] = _local_references(item.get(field), event_ids, f"candidate.{field}")
        measurements: list[dict[str, object]] = []
        raw_measurements = _array(item.get("measurements"), "candidate.measurements")
        measurement_count += len(raw_measurements)
        if measurement_count > policy.max_measurements:
            raise VlmValidationError("persisted measurements exceed the frozen structural budget")
        for raw in raw_measurements:
            measurement = dict(_closed(raw, frozenset({
                "measurement_kind", "value", "confidence", "fact_refs", "event_refs",
            }), "measurement"))
            measurement["fact_refs"] = _local_references(measurement["fact_refs"], fact_ids, "measurement.fact_refs")
            measurement["event_refs"] = _local_references(measurement["event_refs"], event_ids, "measurement.event_refs")
            measurements.append(measurement)
        item["measurements"] = measurements

    wire = {
        "schema_version": 4, "window_summary": summary, "continuity": continuity,
        "entities": wire_entities, "facts": wire_facts, "events": wire_events,
        "candidate_hypotheses": wire_candidates,
    }
    result = _parse_value(
        wire, manifest=manifest, manifest_set=manifest_set, request_identity=request_identity,
        policy=policy, raw_response_sha256=cast(str, provenance["raw_response_sha256"]),
    )
    if result.to_mapping() != root:
        raise VlmValidationError("persisted v4 pack is not the exact canonical derived mapping")
    return result


def decode_vlm_semantic_pack_v4(
    value: object, *, manifest: WindowManifest, manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity, policy: VlmParsePolicy,
) -> VlmSemanticPackV4:
    """Verify a v4 mapping against context; raw-Blob authenticity remains external."""
    _validate_context(manifest, manifest_set, request_identity, policy)
    try:
        return _decode_value(
            value, manifest=manifest, manifest_set=manifest_set,
            request_identity=request_identity, policy=policy,
        )
    except (VlmResponseRejected, VlmResponseIndeterminate, UnicodeEncodeError, RecursionError) as error:
        raise VlmValidationError(f"invalid persisted v4 semantic pack: {error}") from error
