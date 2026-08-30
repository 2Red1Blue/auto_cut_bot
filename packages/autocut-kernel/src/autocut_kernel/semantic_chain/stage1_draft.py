"""Closed, untrusted cross-window intent; never Stage 1 authority or admission.

References name a global object in its owning committed *pack* window, not its
coarse support/core-owner window. Arrays are sets normalized by ID/reference;
empty arrays do not establish coverage. Merge evidence is checked for existence,
not truth: a decoded proposal does not merge entities. The Store owns durable
provenance verification; this pure boundary binds its exact returned projection.
Public dataclass construction is untrusted content, not proof of decoding. A
future compiler must re-decode audited raw bytes against Store-returned inputs;
it must never treat the Stage1Draft Python type as an authority capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Literal, cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..store.models import (
    BlobRef,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
)
from .core_observations import semantic_pack

STAGE1_DRAFT_SCHEMA_VERSION = "stage1-cross-window-draft-v1"
_ID_PATTERN = r"[a-z][a-z0-9_]{0,63}"
_HASH_PATTERN = r"sha256:(?!0{64}$)[0-9a-f]{64}"
_PHASES = ("setup", "escalation", "turn", "reveal", "payoff", "consequence", "coda")
_MAX_JSON_DEPTH = 16
_ObjectType = Literal["entity", "fact", "event"]


class Stage1DraftError(ValueError):
    """Malformed, excessive, or input-unbound draft content."""


@dataclass(frozen=True, slots=True)
class Stage1DraftPolicy:
    max_response_bytes: int
    max_prompt_bytes: int
    max_input_windows: int
    max_input_objects: int
    max_beats: int
    max_obligations: int
    max_story_threads: int
    max_merge_proposals: int
    max_references_per_item: int
    max_text_characters: int
    max_total_text_characters: int

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= 2**53 - 1:  # noqa: E721
                raise Stage1DraftError("draft policy bounds must be positive exact integers")

    def to_mapping(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True, order=True)
class Stage1DraftEvidenceRef:
    window_manifest_sha256: str
    object_type: _ObjectType
    object_id: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "window_manifest_sha256": self.window_manifest_sha256,
            "object_type": self.object_type,
            "object_id": self.object_id,
        }


@dataclass(frozen=True, slots=True)
class Stage1DraftBeat:
    beat_id: str
    summary: str
    phase: str
    event_refs: tuple[Stage1DraftEvidenceRef, ...]
    obligation_ids: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "beat_id": self.beat_id,
            "summary": self.summary,
            "phase": self.phase,
            "event_refs": [ref.to_mapping() for ref in self.event_refs],
            "obligation_ids": list(self.obligation_ids),
        }


@dataclass(frozen=True, slots=True)
class Stage1DraftObligation:
    obligation_id: str
    description: str
    required_fact_refs: tuple[Stage1DraftEvidenceRef, ...]
    success_criteria: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "obligation_id": self.obligation_id,
            "description": self.description,
            "required_fact_refs": [ref.to_mapping() for ref in self.required_fact_refs],
            "success_criteria": self.success_criteria,
        }


@dataclass(frozen=True, slots=True)
class Stage1DraftStoryThread:
    story_thread_id: str
    title: str
    premise: str
    obligation_ids: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "story_thread_id": self.story_thread_id,
            "title": self.title,
            "premise": self.premise,
            "obligation_ids": list(self.obligation_ids),
        }


@dataclass(frozen=True, slots=True)
class Stage1DraftMergeProposal:
    merge_id: str
    entity_refs: tuple[Stage1DraftEvidenceRef, ...]
    evidence_refs: tuple[Stage1DraftEvidenceRef, ...]
    rationale: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "merge_id": self.merge_id,
            "rationale": self.rationale,
            "entity_refs": [ref.to_mapping() for ref in self.entity_refs],
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
        }


@dataclass(frozen=True, slots=True)
class Stage1Draft:
    input_binding_sha256: str
    beats: tuple[Stage1DraftBeat, ...]
    obligations: tuple[Stage1DraftObligation, ...]
    story_threads: tuple[Stage1DraftStoryThread, ...]
    merge_proposals: tuple[Stage1DraftMergeProposal, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": STAGE1_DRAFT_SCHEMA_VERSION,
            "input_binding_sha256": self.input_binding_sha256,
            "beats": [item.to_mapping() for item in self.beats],
            "obligations": [item.to_mapping() for item in self.obligations],
            "story_threads": [item.to_mapping() for item in self.story_threads],
            "merge_proposals": [item.to_mapping() for item in self.merge_proposals],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def _closed(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise Stage1DraftError("draft object has missing or unknown fields")
    mapping = cast(dict[str, object], value)
    if set(mapping) != set(keys):
        raise Stage1DraftError("draft object has missing or unknown fields")
    return mapping


def _array(value: object, maximum: int, *, minimum: int = 0) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise Stage1DraftError("draft array violates its count bound")
    items = cast(list[object], value)
    if not minimum <= len(items) <= maximum:
        raise Stage1DraftError("draft array violates its count bound")
    return items


def _string(value: object, pattern: str) -> str:
    if type(value) is not str or re.fullmatch(pattern, value) is None:  # noqa: E721
        raise Stage1DraftError("draft identifier is invalid")
    return value


def _blob_mapping(blob: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(blob.object_id),
        "content_hash": blob.content_hash,
        "byte_length": blob.byte_length,
        "media_type": blob.media_type,
    }


def _member_binding(
    item: CommittedVlmSemanticInput, inputs: CommittedSemanticInputs
) -> dict[str, object]:
    source, identity = item.source_window, item.request_identity
    persisted = item.semantic_pack
    pack, child, response = semantic_pack(item), persisted.source_child, item.response_record
    if (
        source.window_manifest_sha256 != identity.window_manifest_sha256
        or source.window_manifest_sha256 != pack.window_manifest_sha256
        or source.window_manifest_sha256 != child.window_manifest_sha256
        or source.window_manifest_set_sha256 != identity.window_manifest_set_sha256
        or source.window_manifest_set_sha256 != child.window_manifest_set_sha256
        or source.source_id != identity.source_id
        or source.source_sha256 != identity.source_sha256
        or source.source_clock_id != identity.source_clock_id
        or source.episode_index != child.episode_index
        or pack.request_identity_sha256 != identity.canonical_hash
        or child.request_identity_sha256 != identity.canonical_hash
        or child.source_manifest_sha256 != inputs.source_manifest.reference.content_hash
        or child.source_provenance_sha256 != inputs.source_manifest.canonical_hash
        or child.request_payload.content_hash != identity.request_payload_sha256
        or child.request_policy != inputs.vlm_aggregate_policy
        or item.raw_response.content_hash != pack.raw_response_sha256
        or response.receipt_id != child.receipt_id
        or response.artifact_set_id != child.artifact_set_id
        or response.scope != persisted.reference.scope
        or response.revision != persisted.reference.revision
        or child.reference.revision != persisted.reference.revision
        or child.source_job != inputs.source_manifest.source_job
        or persisted.reference.scope != inputs.source_manifest.reference.scope
        or inputs.vlm_semantic_pack_set.scope != persisted.reference.scope
        or response.artifact_type != "vlm_response_record"
        or response.member_ordinal != 1
        or response.logical_id != f"vlm_response_{source.window_manifest_sha256[7:31]}"
    ):
        raise Stage1DraftError("committed VLM input identities do not close")
    pack_ref = CommittedArtifactMemberReference(
        child.receipt_id,
        child.artifact_set_id,
        2,
        persisted.reference.scope,
        "vlm_semantic_pack",
        persisted.reference.logical_id,
        persisted.reference.revision,
        persisted.reference.content_hash,
    )
    request_ref = CommittedArtifactMemberReference(
        child.receipt_id,
        child.artifact_set_id,
        0,
        child.reference.scope,
        "vlm_request_record",
        child.reference.logical_id,
        child.reference.revision,
        child.reference.content_hash,
    )
    return {
        "request_record": request_ref.to_mapping(),
        "request_identity": identity.to_mapping(),
        "generation_child": child.to_mapping(),
        "generation_owner": {
            "job_key": child.source_job.job_key,
            "profile": child.source_job.profile,
            "kernel_job_id": str(child.kernel_job_id),
            "command_slot_id": str(child.command_slot_id),
        },
        "response_record": response.to_mapping(),
        "raw_response": _blob_mapping(item.raw_response),
        "semantic_pack": pack_ref.to_mapping(),
        "semantic_pack_sha256": pack.canonical_hash,
        "source_window": {
            "episode_index": source.episode_index,
            "stream_index": source.stream_index,
            "core_start_pts": source.core_start_pts,
            "core_end_pts": source.core_end_pts,
            "window_manifest_sha256": source.window_manifest_sha256,
            "window_manifest_set_sha256": source.window_manifest_set_sha256,
            "source_id": source.source_id,
            "source_sha256": source.source_sha256,
            "source_clock_id": source.source_clock_id,
            "proxy_blob": _blob_mapping(source.proxy_blob),
        },
    }


def _catalog(
    inputs: CommittedSemanticInputs, policy: Stage1DraftPolicy
) -> tuple[str, frozenset[Stage1DraftEvidenceRef]]:
    if type(inputs) is not CommittedSemanticInputs or type(policy) is not Stage1DraftPolicy:  # noqa: E721
        raise Stage1DraftError("draft requires committed inputs and an explicit policy")
    if len(inputs.inputs) > policy.max_input_windows:
        raise Stage1DraftError("committed window count exceeds draft policy")
    count = sum(
        len(values)
        for item in inputs.inputs
        for values in (
            semantic_pack(item).entities,
            semantic_pack(item).facts,
            semantic_pack(item).events,
        )
    )
    if count > policy.max_input_objects:
        raise Stage1DraftError("committed object count exceeds draft policy")
    refs: set[Stage1DraftEvidenceRef] = set()
    for item in inputs.inputs:
        pack = semantic_pack(item)
        for kind, ids in (
            ("entity", tuple(value.entity_id for value in pack.entities)),
            ("fact", tuple(value.fact_id for value in pack.facts)),
            ("event", tuple(value.event_id for value in pack.events)),
        ):
            for object_id in ids:
                ref = Stage1DraftEvidenceRef(
                    pack.window_manifest_sha256, cast(_ObjectType, kind), object_id
                )
                if ref in refs:
                    raise Stage1DraftError("committed object reference is duplicated")
                refs.add(ref)
    binding = canonical_json_hash(
        {
            "schema_version": "stage1-draft-input-binding-v1",
            "source_manifest": inputs.source_manifest.provenance_mapping(),
            "source_grant_sha256": inputs.source_grant.canonical_hash,
            "vlm_semantic_pack_set": inputs.vlm_semantic_pack_set.to_mapping(),
            "vlm_aggregate_policy": inputs.vlm_aggregate_policy.to_mapping(),
            "vlm_members": [_member_binding(item, inputs) for item in inputs.inputs],
        }
    )
    return binding, frozenset(refs)


def stage1_draft_prompt_inputs(
    inputs: CommittedSemanticInputs, *, policy: Stage1DraftPolicy
) -> dict[str, object]:
    """Fresh semantic-only projection and exact reference allowlist for generation."""
    binding, refs = _catalog(inputs, policy)
    windows: list[dict[str, object]] = []
    for item in inputs.inputs:
        pack = semantic_pack(item)
        windows.append(
            {
                "window_manifest_sha256": pack.window_manifest_sha256,
                "window_summary": {
                    "summary": pack.window_summary.summary,
                    "dominant_temporal_mode": pack.window_summary.dominant_temporal_mode.value,
                    "fact_refs": list(pack.window_summary.fact_refs),
                    "event_refs": list(pack.window_summary.event_refs),
                },
                "continuity": {
                    "starts_mid_event": pack.continuity.starts_mid_event,
                    "ends_mid_event": pack.continuity.ends_mid_event,
                    "continues_from_previous": pack.continuity.continues_from_previous,
                    "continues_into_next": pack.continuity.continues_into_next,
                    "entry_state_fact_refs": list(pack.continuity.entry_state_fact_refs),
                    "exit_state_fact_refs": list(pack.continuity.exit_state_fact_refs),
                },
                "entities": [
                    {
                        "entity_id": value.entity_id,
                        "entity_kind": value.entity_kind.value,
                        "display_label": value.display_label,
                        "visual_description": value.visual_description,
                    }
                    for value in pack.entities
                ],
                "facts": [
                    {
                        "fact_id": value.fact_id,
                        "fact_kind": value.fact_kind.value,
                        "subject_ref": value.subject_ref,
                        "object_ref": value.object_ref,
                        "summary": value.summary,
                    }
                    for value in pack.facts
                ],
                "events": [
                    {
                        "event_id": value.event_id,
                        "event_kind": value.event_kind.value,
                        "summary": value.summary,
                        "participant_refs": list(value.participant_refs),
                        "fact_refs": list(value.fact_refs),
                        "cause_event_refs": list(value.cause_event_refs),
                        "effect_event_refs": list(value.effect_event_refs),
                        "open_question": value.open_question,
                        "temporal_mode": value.temporal_mode.value,
                    }
                    for value in pack.events
                ],
            }
        )
    projection: dict[str, object] = {
        "schema_version": STAGE1_DRAFT_SCHEMA_VERSION,
        "input_binding_sha256": binding,
        "allowed_refs": [ref.to_mapping() for ref in sorted(refs)],
        "windows": windows,
    }
    if len(canonical_json_bytes(projection)) > policy.max_prompt_bytes:
        raise Stage1DraftError("semantic prompt projection exceeds its byte bound")
    return projection


def _bounded_json(raw: bytes, policy: Stage1DraftPolicy) -> object:
    if type(raw) is not bytes or not 0 < len(raw) <= policy.max_response_bytes:  # noqa: E721
        raise Stage1DraftError("draft response violates its byte bound")
    # Depth guard before the shared strict parser/canonicalizer recurses. This is
    # only a resource guard; JSON syntax and escaping are still parser-owned.
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise Stage1DraftError("draft JSON nesting exceeds its bound")
        elif byte in (93, 125):
            depth -= 1
    try:
        value, _canonical = load_canonical_json_bytes(raw, origin="Stage 1 draft")
        return cast(object, value)
    except (ValueError, RecursionError) as error:
        raise Stage1DraftError("draft response must be strict UTF-8 JSON") from error


def decode_stage1_draft(
    raw: bytes, *, inputs: CommittedSemanticInputs, policy: Stage1DraftPolicy
) -> Stage1Draft:
    """Validate content/reference closure only; no rule or coverage result exists."""
    if type(policy) is not Stage1DraftPolicy:  # noqa: E721
        raise Stage1DraftError("draft requires an explicit policy")
    root = _closed(
        _bounded_json(raw, policy),
        (
            "schema_version",
            "input_binding_sha256",
            "beats",
            "obligations",
            "story_threads",
            "merge_proposals",
        ),
    )
    binding, allowed = _catalog(inputs, policy)
    if (
        root["schema_version"] != STAGE1_DRAFT_SCHEMA_VERSION
        or root["input_binding_sha256"] != binding
    ):
        raise Stage1DraftError("draft version or committed input binding is invalid")
    text_count = 0

    def text(value: object) -> str:
        nonlocal text_count
        if type(value) is not str or not value.strip() or len(value) > policy.max_text_characters:  # noqa: E721
            raise Stage1DraftError("draft text violates its bound")
        text_count += len(value)
        if text_count > policy.max_total_text_characters:
            raise Stage1DraftError("draft total text exceeds its bound")
        return value

    def refs(
        value: object, kinds: tuple[_ObjectType, ...], *, minimum: int = 1
    ) -> tuple[Stage1DraftEvidenceRef, ...]:
        result: list[Stage1DraftEvidenceRef] = []
        for raw_ref in _array(value, policy.max_references_per_item, minimum=minimum):
            ref = _closed(raw_ref, ("window_manifest_sha256", "object_type", "object_id"))
            if ref["object_type"] not in kinds:
                raise Stage1DraftError("draft evidence reference has the wrong object type")
            decoded = Stage1DraftEvidenceRef(
                _string(ref["window_manifest_sha256"], _HASH_PATTERN),
                ref["object_type"],
                _string(ref["object_id"], _HASH_PATTERN),
            )
            if decoded not in allowed or decoded in result:
                raise Stage1DraftError("draft evidence is unknown, misowned, or duplicated")
            result.append(decoded)
        return tuple(sorted(result))

    def records(
        name: str, maximum: int, id_key: str, keys: tuple[str, ...]
    ) -> list[dict[str, object]]:
        result = [_closed(value, keys) for value in _array(root[name], maximum)]
        ids = [_string(item[id_key], _ID_PATTERN) for item in result]
        if len(ids) != len(set(ids)):
            raise Stage1DraftError("draft local identities must be unique within each type")
        return sorted(result, key=lambda item: cast(str, item[id_key]))

    obligations = tuple(
        Stage1DraftObligation(
            cast(str, item["obligation_id"]),
            text(item["description"]),
            refs(item["required_fact_refs"], ("fact",)),
            text(item["success_criteria"]),
        )
        for item in records(
            "obligations",
            policy.max_obligations,
            "obligation_id",
            (
                "obligation_id",
                "description",
                "required_fact_refs",
                "success_criteria",
            ),
        )
    )
    obligation_ids = {item.obligation_id for item in obligations}

    def obligation_refs(value: object) -> tuple[str, ...]:
        ids = tuple(
            _string(item, _ID_PATTERN)
            for item in _array(value, policy.max_references_per_item, minimum=1)
        )
        if len(ids) != len(set(ids)) or not set(ids) <= obligation_ids:
            raise Stage1DraftError("draft obligation references are unknown or duplicated")
        return tuple(sorted(ids))

    beats: list[Stage1DraftBeat] = []
    for item in records(
        "beats",
        policy.max_beats,
        "beat_id",
        ("beat_id", "summary", "phase", "event_refs", "obligation_ids"),
    ):
        if item["phase"] not in _PHASES:
            raise Stage1DraftError("draft beat phase is unsupported")
        beats.append(
            Stage1DraftBeat(
                cast(str, item["beat_id"]),
                text(item["summary"]),
                cast(str, item["phase"]),
                refs(item["event_refs"], ("event",)),
                obligation_refs(item["obligation_ids"]),
            )
        )
    threads = tuple(
        Stage1DraftStoryThread(
            cast(str, item["story_thread_id"]),
            text(item["title"]),
            text(item["premise"]),
            obligation_refs(item["obligation_ids"]),
        )
        for item in records(
            "story_threads",
            policy.max_story_threads,
            "story_thread_id",
            (
                "story_thread_id",
                "title",
                "premise",
                "obligation_ids",
            ),
        )
    )
    merges: list[Stage1DraftMergeProposal] = []
    for item in records(
        "merge_proposals",
        policy.max_merge_proposals,
        "merge_id",
        ("merge_id", "entity_refs", "evidence_refs", "rationale"),
    ):
        entities = refs(item["entity_refs"], ("entity",), minimum=2)
        if len({ref.window_manifest_sha256 for ref in entities}) < 2:
            raise Stage1DraftError("cross-window merge must name entities in distinct windows")
        merges.append(
            Stage1DraftMergeProposal(
                cast(str, item["merge_id"]),
                entities,
                refs(item["evidence_refs"], ("fact", "event")),
                text(item["rationale"]),
            )
        )
    return Stage1Draft(binding, tuple(beats), obligations, threads, tuple(merges))


def stage1_draft_response_schema(policy: Stage1DraftPolicy) -> dict[str, object]:
    """Fresh closed shape/count schema; semantic closure and total budget are decoder-owned."""
    if type(policy) is not Stage1DraftPolicy:  # noqa: E721
        raise Stage1DraftError("draft schema requires an explicit policy")

    def record(properties: dict[str, object]) -> dict[str, object]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }

    def array(item: dict[str, object], maximum: int, minimum: int = 0) -> dict[str, object]:
        return {
            "type": "array",
            "items": item,
            "minItems": minimum,
            "maxItems": maximum,
            "uniqueItems": True,
        }

    def identifier() -> dict[str, object]:
        return {"type": "string", "pattern": "^" + _ID_PATTERN + "$", "maxLength": 64}

    def digest() -> dict[str, object]:
        return {
            "type": "string",
            "pattern": "^" + _HASH_PATTERN + "$",
            "minLength": 71,
            "maxLength": 71,
        }

    def text() -> dict[str, object]:
        return {
            "type": "string",
            "minLength": 1,
            "maxLength": policy.max_text_characters,
            "pattern": r"\S",
        }

    def refs(kinds: tuple[str, ...], minimum: int = 1) -> dict[str, object]:
        return array(
            record(
                {
                    "window_manifest_sha256": digest(),
                    "object_type": {"enum": list(kinds)},
                    "object_id": digest(),
                }
            ),
            policy.max_references_per_item,
            minimum,
        )

    def obligations() -> dict[str, object]:
        return array(identifier(), policy.max_references_per_item, 1)

    result = record(
        {
            "schema_version": {"const": STAGE1_DRAFT_SCHEMA_VERSION},
            "input_binding_sha256": digest(),
            "beats": array(
                record(
                    {
                        "beat_id": identifier(),
                        "summary": text(),
                        "phase": {"enum": list(_PHASES)},
                        "event_refs": refs(("event",)),
                        "obligation_ids": obligations(),
                    }
                ),
                policy.max_beats,
            ),
            "obligations": array(
                record(
                    {
                        "obligation_id": identifier(),
                        "description": text(),
                        "required_fact_refs": refs(("fact",)),
                        "success_criteria": text(),
                    }
                ),
                policy.max_obligations,
            ),
            "story_threads": array(
                record(
                    {
                        "story_thread_id": identifier(),
                        "title": text(),
                        "premise": text(),
                        "obligation_ids": obligations(),
                    }
                ),
                policy.max_story_threads,
            ),
            "merge_proposals": array(
                record(
                    {
                        "merge_id": identifier(),
                        "entity_refs": refs(("entity",), 2),
                        "evidence_refs": refs(("fact", "event")),
                        "rationale": text(),
                    }
                ),
                policy.max_merge_proposals,
            ),
        }
    )
    result["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return result
