"""Immutable full-predecessor context values, without Store/admission authority.

Full member payloads live once in a shared pool, never once per requirement or
Story. A requirement inherits the whole pool of its closure set: this registered
strategy deliberately uses a complete superset, not a selective dependency walk.
Payload JSON is immutable canonical text in memory and fresh JSON on the wire;
its original member hash uses the Store-owned payload hashing algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..store.models import ArtifactMember, canonical_payload_hash
from .editorial_models import (
    editorial_array,
    editorial_hash,
    editorial_integer,
    editorial_mapping,
    editorial_text,
    editorial_tuple,
)
from .member_refs import SemanticMemberIdentity, SemanticObjectRef

CONTEXT_STRATEGY = "unpartitioned-batch-v1"
_MAX_BYTES = 64 * 1024 * 1024
_MAX_MEMBER_BYTES = 16 * 1024 * 1024


class EditorialContextError(ValueError):
    """Context content is malformed, inconsistent or beyond explicit limits."""


@dataclass(frozen=True, slots=True)
class EditorialContextPolicy:
    strategy: str
    budget_unit: str
    max_story_context_bytes: int
    max_batch_context_bytes: int
    max_source_members: int

    def __post_init__(self) -> None:
        if type(self.strategy) is not str or self.strategy != CONTEXT_STRATEGY:  # noqa: E721
            raise EditorialContextError("only the complete unpartitioned batch strategy is registered")
        if type(self.budget_unit) is not str or self.budget_unit != "bytes":  # noqa: E721
            raise EditorialContextError("context budgets must measure bytes, not tokens or cost")
        for value in (self.max_story_context_bytes, self.max_batch_context_bytes):
            if not 1 <= editorial_integer(value, minimum=1) <= _MAX_BYTES:
                raise EditorialContextError("context byte limit exceeds implementation ceiling")
        if not 1 <= editorial_integer(self.max_source_members, minimum=1) <= 8192:
            raise EditorialContextError("context predecessor member count exceeds implementation ceiling")

    def to_mapping(self) -> dict[str, object]:
        return {field.name: cast(object, getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialContextPolicy:
        item = editorial_mapping(value, tuple(field.name for field in fields(cls)))
        return cls(cast(str, item["strategy"]), cast(str, item["budget_unit"]),
                   cast(int, item["max_story_context_bytes"]), cast(int, item["max_batch_context_bytes"]),
                   cast(int, item["max_source_members"]))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ExactContextMember:
    member_ref: SemanticMemberIdentity
    payload_json: str

    def __post_init__(self) -> None:
        if type(self.member_ref) is not SemanticMemberIdentity:  # noqa: E721
            raise EditorialContextError("context member needs an exact semantic member identity")
        editorial_hash(self.member_ref.content_hash)
        raw = editorial_text(self.payload_json).encode("utf-8")
        if len(raw) > _MAX_MEMBER_BYTES:
            raise EditorialContextError("context member exceeds implementation byte ceiling")
        value, canonical = load_canonical_json_bytes(raw, origin="editorial context member")
        if type(value) is not dict or canonical != raw:  # noqa: E721
            raise EditorialContextError("context member must retain a canonical complete object payload")
        if canonical_payload_hash(self.payload_json) != self.member_ref.content_hash:
            raise EditorialContextError("context payload does not match its exact member hash")

    def to_mapping(self) -> dict[str, object]:
        return {"member_ref": self.member_ref.to_mapping(), "payload": load_canonical_json_bytes(
            self.payload_json.encode("utf-8"), origin="editorial context member",
        )[0]}

    @classmethod
    def from_mapping(cls, value: object) -> ExactContextMember:
        item = editorial_mapping(value, ("member_ref", "payload"))
        return cls(SemanticMemberIdentity.from_mapping(item["member_ref"]), canonical_json_bytes(item["payload"]).decode("utf-8"))

    @classmethod
    def from_artifact_member(cls, member: ArtifactMember) -> ExactContextMember:
        identity = SemanticMemberIdentity.from_artifact_member(member)
        _, canonical = load_canonical_json_bytes(member.payload_json.encode("utf-8"), origin="editorial predecessor")
        return cls(identity, canonical.decode("utf-8"))

    def as_artifact_member(self) -> ArtifactMember:
        ref = self.member_ref
        return ArtifactMember(ref.artifact_type, ref.logical_id, ref.revision, ref.scope, ref.content_hash, self.payload_json)

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class MaterialEvidenceClosure:
    source_material_requirement_id: str
    obligation_ref: SemanticObjectRef
    required_fact_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        editorial_text(self.source_material_requirement_id)
        if (type(self.obligation_ref) is not SemanticObjectRef  # noqa: E721
                or self.obligation_ref.member_ref.artifact_type != "narrative_graph"
                or self.obligation_ref.object_type != "obligation"):
            raise EditorialContextError("material closure needs an exact Graph obligation")
        editorial_tuple(self.required_fact_refs, SemanticObjectRef)
        if len(set(self.required_fact_refs)) != len(self.required_fact_refs):
            raise EditorialContextError("material closure duplicates a Fact")
        for ref in self.required_fact_refs:
            if ref.member_ref != self.obligation_ref.member_ref or ref.object_type != "fact":
                raise EditorialContextError("material closure Fact has a foreign owner/type")

    def to_mapping(self) -> dict[str, object]:
        return {"source_material_requirement_id": self.source_material_requirement_id,
                "obligation_ref": self.obligation_ref.to_mapping(),
                "required_fact_refs": [ref.to_mapping() for ref in self.required_fact_refs]}

    @classmethod
    def from_mapping(cls, value: object) -> MaterialEvidenceClosure:
        item = editorial_mapping(value, ("source_material_requirement_id", "obligation_ref", "required_fact_refs"))
        return cls(editorial_text(item["source_material_requirement_id"]), SemanticObjectRef.from_mapping(item["obligation_ref"]),
                   editorial_array(item["required_fact_refs"], SemanticObjectRef.from_mapping))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EvidenceClosureSet:
    story_id: str
    proposal_ref: SemanticObjectRef
    member_refs: tuple[SemanticMemberIdentity, ...]
    requirements: tuple[MaterialEvidenceClosure, ...]

    def __post_init__(self) -> None:
        editorial_hash(self.story_id)
        if (type(self.proposal_ref) is not SemanticObjectRef or self.proposal_ref.object_type != "proposal"  # noqa: E721
                or self.proposal_ref.member_ref.artifact_type != "proposal_set"):
            raise EditorialContextError("evidence closure requires the exact selected Proposal")
        editorial_tuple(self.member_refs, SemanticMemberIdentity, nonempty=True)
        if len(set(self.member_refs)) != len(self.member_refs) or self.proposal_ref.member_ref not in self.member_refs:
            raise EditorialContextError("closure member universe is duplicated or misses its ProposalSet")
        if any(ref.scope != self.proposal_ref.member_ref.scope for ref in self.member_refs):
            raise EditorialContextError("closure member universe mixes scopes")
        editorial_tuple(self.requirements, MaterialEvidenceClosure, nonempty=True)
        if len({item.source_material_requirement_id for item in self.requirements}) != len(self.requirements):
            raise EditorialContextError("closure repeats a material requirement")
        if any(item.obligation_ref.member_ref not in self.member_refs for item in self.requirements):
            raise EditorialContextError("closure roots have no included Graph owner")

    def to_mapping(self) -> dict[str, object]:
        return {"schema_version": "stage3-evidence-closure-set-v1", "closure_strategy": "full-predecessor-pool-v1",
                "story_id": self.story_id, "proposal_ref": self.proposal_ref.to_mapping(),
                "member_refs": [ref.to_mapping() for ref in self.member_refs],
                "requirements": [item.to_mapping() for item in self.requirements]}

    @classmethod
    def from_mapping(cls, value: object) -> EvidenceClosureSet:
        item = editorial_mapping(value, ("schema_version", "closure_strategy", "story_id", "proposal_ref", "member_refs", "requirements"))
        if item["schema_version"] != "stage3-evidence-closure-set-v1" or item["closure_strategy"] != "full-predecessor-pool-v1":
            raise EditorialContextError("unsupported complete evidence closure strategy/version")
        return cls(editorial_hash(item["story_id"]), SemanticObjectRef.from_mapping(item["proposal_ref"]),
                   editorial_array(item["member_refs"], SemanticMemberIdentity.from_mapping),
                   editorial_array(item["requirements"], MaterialEvidenceClosure.from_mapping))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EditorialContextManifest:
    story_id: str
    proposal_ref: SemanticObjectRef
    closure_set_ref: SemanticMemberIdentity
    context_policy_sha256: str
    context_content_sha256: str
    context_byte_length: int
    byte_limit: int

    def __post_init__(self) -> None:
        for value in (self.story_id, self.context_policy_sha256, self.context_content_sha256):
            editorial_hash(value)
        if (type(self.proposal_ref) is not SemanticObjectRef or self.proposal_ref.object_type != "proposal"  # noqa: E721
                or self.proposal_ref.member_ref.artifact_type != "proposal_set"):
            raise EditorialContextError("context manifest requires an exact Proposal reference")
        if (type(self.closure_set_ref) is not SemanticMemberIdentity  # noqa: E721
                or self.closure_set_ref.artifact_type != "evidence_closure_set"
                or self.closure_set_ref.logical_id != f"evidence_closure_set@{self.story_id}"
                or self.closure_set_ref.scope != self.proposal_ref.member_ref.scope):
            raise EditorialContextError("context manifest names a foreign closure")
        if not 1 <= editorial_integer(self.context_byte_length, minimum=1) <= editorial_integer(self.byte_limit, minimum=1) <= _MAX_BYTES:
            raise EditorialContextError("complete Story context exceeds its explicit byte limit")

    def to_mapping(self) -> dict[str, object]:
        return {"schema_version": "stage3-context-manifest-v1", "strategy": CONTEXT_STRATEGY,
                "story_id": self.story_id, "proposal_ref": self.proposal_ref.to_mapping(),
                "closure_set_ref": self.closure_set_ref.to_mapping(), "context_policy_sha256": self.context_policy_sha256,
                "context_content_sha256": self.context_content_sha256, "context_byte_length": self.context_byte_length,
                "budget": {"unit": "bytes", "limit": self.byte_limit}}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialContextManifest:
        item = editorial_mapping(value, ("schema_version", "strategy", "story_id", "proposal_ref", "closure_set_ref",
                                        "context_policy_sha256", "context_content_sha256", "context_byte_length", "budget"))
        budget = editorial_mapping(item["budget"], ("unit", "limit"))
        if (item["schema_version"] != "stage3-context-manifest-v1" or item["strategy"] != CONTEXT_STRATEGY
                or budget["unit"] != "bytes"):
            raise EditorialContextError("unsupported context manifest version/strategy/budget")
        return cls(editorial_hash(item["story_id"]), SemanticObjectRef.from_mapping(item["proposal_ref"]),
                   SemanticMemberIdentity.from_mapping(item["closure_set_ref"]), editorial_hash(item["context_policy_sha256"]),
                   editorial_hash(item["context_content_sha256"]), editorial_integer(item["context_byte_length"], minimum=1),
                   editorial_integer(budget["limit"], minimum=1))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class StoryEditorialContext:
    story_id: str
    closure_member: ArtifactMember
    context_member: ArtifactMember

    def __post_init__(self) -> None:
        editorial_hash(self.story_id)
        for kind, member in (("evidence_closure_set", self.closure_member), ("context_manifest", self.context_member)):
            ref = SemanticMemberIdentity.from_artifact_member(member)
            if ref.artifact_type != kind or ref.logical_id != f"{kind}@{self.story_id}":
                raise EditorialContextError("Story context member has foreign type/logical identity")
        closure, manifest = self.closure, self.manifest
        if (closure.story_id != self.story_id or manifest.story_id != self.story_id
                or closure.proposal_ref != manifest.proposal_ref
                or manifest.closure_set_ref != SemanticMemberIdentity.from_artifact_member(self.closure_member)
                or self.context_member.scope != self.closure_member.scope
                or self.context_member.revision != self.closure_member.revision):
            raise EditorialContextError("Story closure/manifest identities do not close")

    @property
    def closure(self) -> EvidenceClosureSet:
        return EvidenceClosureSet.from_mapping(load_canonical_json_bytes(self.closure_member.payload_json.encode(), origin="Story closure")[0])

    @property
    def manifest(self) -> EditorialContextManifest:
        return EditorialContextManifest.from_mapping(load_canonical_json_bytes(self.context_member.payload_json.encode(), origin="Story manifest")[0])

    def to_mapping(self) -> dict[str, object]:
        return {"story_id": self.story_id, "closure_member": ExactContextMember.from_artifact_member(self.closure_member).to_mapping(),
                "context_member": ExactContextMember.from_artifact_member(self.context_member).to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> StoryEditorialContext:
        item = editorial_mapping(value, ("story_id", "closure_member", "context_member"))
        return cls(editorial_hash(item["story_id"]), ExactContextMember.from_mapping(item["closure_member"]).as_artifact_member(),
                   ExactContextMember.from_mapping(item["context_member"]).as_artifact_member())

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
