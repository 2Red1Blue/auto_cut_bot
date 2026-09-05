"""Complete, deduplicated Stage 3 context construction before generation.

This pure layer consumes values from the exact predecessor reader, never calls
a Store/provider and never grants commitment. Full Source, VLM request/semantic
members and all Stage 1/2 payloads are retained. A per-Story manifest hashes the
expanded content (common pool + policies + its closure), while the actual batch
contains the common pool only once. No request hash is embedded in its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
    sha256_bytes,
)
from ..source_manifest import decode_source_manifest
from ..store.models import (
    VLM_REQUEST_IDENTITY_FIELDS,
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommittedSemanticInputs,
    canonical_payload_hash,
)
from ..vlm.decoder import decode_vlm_semantic_pack
from ..vlm.models import VlmParsePolicy, VlmRequestIdentity
from ..vlm.semantic_parser_v4 import decode_vlm_semantic_pack_v4
from .candidate_catalog import CandidateCatalogPolicy
from .candidate_projection import CandidateCatalogProjection, project_candidate_catalog
from .core_observations import CoreSemanticPack
from .derived_input_binding import derived_record
from .editorial_context_models import (
    EditorialContextError,
    EditorialContextManifest,
    EditorialContextPolicy,
    EvidenceClosureSet,
    ExactContextMember,
    MaterialEvidenceClosure,
    StoryEditorialContext,
)
from .editorial_models import (
    editorial_array,
    editorial_hash,
    editorial_integer,
    editorial_mapping,
    editorial_text,
    editorial_tuple,
)
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import ObligationAttributes
from .portfolio_values import StorySelection
from .stage1_result import STAGE1_MEMBER_TYPES, Stage1Values, decode_stage1_members
from .story_design_context import story_design_input_binding
from .story_design_models import JobPolicy, StoryDesignPolicy
from .story_design_result import STAGE2_MEMBER_TYPES, StoryDesignValues, decode_story_design_members


def _policies(job: JobPolicy, story: StoryDesignPolicy, candidate: CandidateCatalogPolicy) -> dict[str, object]:
    return {"job_policy": job.to_mapping(), "story_policy": story.to_mapping(), "candidate_policy": candidate.to_mapping()}


def _content(pool: tuple[ExactContextMember, ...], policies: dict[str, object], closure: EvidenceClosureSet) -> bytes:
    """The documented exact expansion used by both producer and batch decoder."""
    return canonical_json_bytes({"schema_version": "stage3-story-context-content-v1",
                                 "member_pool": [member.to_mapping() for member in pool],
                                 "stage2_policies": policies, "closure": closure.to_mapping()})


def _uuid(value: object) -> UUID:
    text = editorial_text(value)
    result = UUID(text)
    if str(result) != text:
        raise EditorialContextError("context request UUID must use its canonical string")
    return result


def _blob(value: object) -> BlobRef:
    item = editorial_mapping(value, ("object_id", "content_hash", "byte_length", "media_type"))
    return BlobRef(_uuid(item["object_id"]), editorial_hash(item["content_hash"]),
                   editorial_integer(item["byte_length"]), editorial_text(item["media_type"]))


def _request(member: ExactContextMember) -> tuple[dict[str, object], VlmRequestIdentity, BlobRef]:
    if member.member_ref.artifact_type == "reprocessed_vlm_evidence":
        return _derived_request(member)
    # There is no standalone public request-record decoder. Decode only its
    # closed persisted value shape here, reusing the identity/blob value owners;
    # never fabricate a PersistedVlmGenerationChild or its commit provenance.
    item = editorial_mapping(load_canonical_json_bytes(member.payload_json.encode(), origin="context VLM request")[0], (
        "attempt_id", "episode_index", "idempotency_key", "provider_idempotency_key", "proxy_blob", "request_hash",
        "request_identity", "request_identity_sha256", "request_payload_blob", "source_manifest_sha256",
        "source_provenance_sha256", "window_manifest_set_sha256", "window_manifest_sha256",
    ))
    _uuid(item["attempt_id"])
    editorial_integer(item["episode_index"])
    for name in ("idempotency_key", "provider_idempotency_key"):
        editorial_text(item[name])
    for name in ("request_hash", "request_identity_sha256", "source_manifest_sha256", "source_provenance_sha256",
                 "window_manifest_set_sha256", "window_manifest_sha256"):
        editorial_hash(item[name])
    fields = editorial_mapping(item["request_identity"], tuple(VLM_REQUEST_IDENTITY_FIELDS))
    identity = VlmRequestIdentity(**{name: editorial_hash(value) if name.endswith("_sha256") else editorial_text(value)
                                     for name, value in fields.items()})
    proxy, payload = _blob(item["proxy_blob"]), _blob(item["request_payload_blob"])
    if (item["request_identity_sha256"] != identity.canonical_hash
            or item["window_manifest_sha256"] != identity.window_manifest_sha256
            or item["window_manifest_set_sha256"] != identity.window_manifest_set_sha256
            or payload.content_hash != identity.request_payload_sha256
            or member.member_ref.logical_id != f"vlm_request_{identity.window_manifest_sha256[7:31]}"):
        raise EditorialContextError("context request record differs from its exact identity")
    return item, identity, proxy


def _derived_request(member: ExactContextMember) -> tuple[dict[str, object], VlmRequestIdentity, BlobRef]:
    record = derived_record(member.payload_json)
    if record["schema_version"] != "reprocessed-vlm-evidence-v2":
        raise EditorialContextError("derived v1 lacks exact Stage 3 projection; reprocess locally to v2")
    fields = editorial_mapping(record["request_identity"], tuple(VLM_REQUEST_IDENTITY_FIELDS))
    identity = VlmRequestIdentity(**{name: editorial_hash(value) if name.endswith("_sha256") else editorial_text(value)
                                     for name, value in fields.items()})
    policy = _derived_policy(record)
    proxy = _blob(record["proxy_blob"])
    request = cast(dict[str, object], record["request"])
    request_hash = canonical_json_hash(request)
    if (member.member_ref.logical_id != "reprocessed_vlm_" + request_hash[7:]
            or record["window_manifest_sha256"] != identity.window_manifest_sha256
            or record["window_manifest_set_sha256"] != identity.window_manifest_set_sha256
            or request.get("parent_request_payload_sha256") != identity.request_payload_sha256
            or policy.canonical_hash != identity.parse_policy_sha256
            or canonical_json_hash(record["proxy_blob"]) != identity.proxy_blob_ref_sha256):
        raise EditorialContextError("derived projection differs from its exact request identity")
    # A local content join view, never a synthetic generation request member.
    return {**record, "episode_index": request["episode_index"]}, identity, proxy


def _derived_policy(record: dict[str, object]) -> VlmParsePolicy:
    fields = editorial_mapping(record["parse_policy"], tuple(VlmParsePolicy.__dataclass_fields__))
    return VlmParsePolicy(**{key: editorial_integer(value, minimum=1) for key, value in fields.items()})


def _raw_packs(pool: tuple[ExactContextMember, ...]) -> tuple[CoreSemanticPack, ...]:
    requests = tuple(_request(member) for member in pool[1:-13:2])
    source = decode_source_manifest(pool[0].payload_json, tuple(proxy for _, _, proxy in requests))
    packs: list[CoreSemanticPack] = []
    for request, episode, owner, member in zip(requests, source.episodes, pool[1:-13:2], pool[2:-13:2], strict=True):
        record, identity, _ = request
        raw = load_canonical_json_bytes(member.payload_json.encode(), origin="context VLM pack")[0]
        if owner.member_ref.artifact_type == "reprocessed_vlm_evidence":
            pack = decode_vlm_semantic_pack_v4(raw, manifest=episode.manifest, manifest_set=episode.manifest_set,
                                             request_identity=identity, policy=_derived_policy(record))
            request_fields = cast(dict[str, object], record["request"])
            if raw != record["semantic_pack"] or pack.raw_response_sha256 != request_fields["parent_raw_response_sha256"]:
                raise EditorialContextError("derived provenance differs from its exact V4 semantic member")
            packs.append(pack)
        else:
            if type(raw) is dict and raw.get("schema_version") == 4:
                raise EditorialContextError("generation V4 lacks persisted parse policy; explicit derived v2 projection required")
            packs.append(decode_vlm_semantic_pack(raw))
    return tuple(packs)


def _raw_pool(
    pool: tuple[ExactContextMember, ...], packs: tuple[CoreSemanticPack, ...], source_grant_sha256: str,
) -> set[SemanticObjectRef]:
    requests = tuple(_request(member) for member in pool[1:-13:2])
    source = decode_source_manifest(pool[0].payload_json, tuple(proxy for _, _, proxy in requests))
    source.census.require_purpose("render_source")
    raw_objects = {SemanticObjectRef(pool[0].member_ref, "source", entry.source_id) for entry in source.census.sources}
    # Source/record/pack windows follow the prepared census order. This is the
    # same complete episode-window universe used by candidate source decoding.
    provenance: set[str] = set()
    for ordinal, (request, pack, episode, request_member, pack_member) in enumerate(zip(
        requests, packs, source.episodes, pool[1:-13:2], pool[2:-13:2], strict=True,
    )):
        record, identity, _ = request
        identity.assert_manifest_binding(episode.manifest, episode.manifest_set)
        expected_pack_id = ("reprocessed_semantic_pack_" + canonical_json_hash(record["request"])[7:]
                            if request_member.member_ref.artifact_type == "reprocessed_vlm_evidence"
                            else f"semantic_pack_{pack.window_manifest_sha256[7:39]}")
        if (record["episode_index"] != ordinal or record["source_manifest_sha256"] != pool[0].member_ref.content_hash
                or pack.request_identity_sha256 != identity.canonical_hash
                or pack.window_manifest_sha256 != identity.window_manifest_sha256
                or pack_member.member_ref.logical_id != expected_pack_id
                or pack_member.member_ref.revision != request_member.member_ref.revision):
            raise EditorialContextError("context request/pack/Source window pair does not close")
        provenance.add(editorial_hash(record["source_provenance_sha256"]))
        raw_objects.add(SemanticObjectRef(pool[0].member_ref, "source_window", pack.window_manifest_sha256))
        for kind, identifiers in (
            ("vlm_entity", (item.entity_id for item in pack.entities)),
            ("vlm_fact", (item.fact_id for item in pack.facts)),
            ("vlm_event", (item.event_id for item in pack.events)),
            ("vlm_candidate", (item.candidate_id for item in pack.candidate_hypotheses)),
        ):
            raw_objects.update(SemanticObjectRef(pack_member.member_ref, kind, identifier) for identifier in identifiers)
    # Agreement is a content join, not verification of the referenced Store
    # provenance/attempt/request body, which are audited by the committed reader.
    if len(provenance) != 1:
        raise EditorialContextError("context requests disagree on Source provenance")
    if source_grant_sha256 != source.census.canonical_hash:
        raise EditorialContextError("context Catalog differs from the actual Source grant")
    return raw_objects


def _reference_owners(pool: tuple[ExactContextMember, ...], raw_objects: set[SemanticObjectRef]) -> None:
    owners = {member.member_ref for member in pool}
    pending: list[object] = [member.to_mapping()["payload"] for member in pool[-13:]]
    # These thirteen payloads have already passed their closed typed decoders.
    # Walk all nested semantic references, including state/diagnostic/candidate
    # evidence, rather than maintaining an incomplete second field allowlist.
    while pending:
        value = pending.pop()
        if type(value) is dict:  # noqa: E721
            mapping = cast(dict[str, object], value)
            if set(mapping) == {"member_ref", "object_type", "object_id"}:
                ref = SemanticObjectRef.from_mapping(mapping)
                if ref.member_ref not in owners or (
                    ref.member_ref.artifact_type in ("whole_series_source_manifest", "vlm_semantic_pack") and ref not in raw_objects
                ):
                    raise EditorialContextError("context semantic evidence does not resolve to the actual pool owner/object")
            else:
                pending.extend(mapping.values())
        elif type(value) is list:  # noqa: E721
            pending.extend(cast(list[object], value))


def _pool_values(pool: tuple[ExactContextMember, ...]) -> tuple[Stage1Values, StoryDesignValues]:
    editorial_tuple(pool, ExactContextMember, nonempty=True)
    if len(pool) < 16:
        raise EditorialContextError("complete context needs Source, VLM request/pack and all thirteen Stage members")
    refs = tuple(member.member_ref for member in pool)
    if (refs[0].artifact_type != "whole_series_source_manifest"
            or tuple(ref.artifact_type for ref in refs[-13:]) != (*STAGE1_MEMBER_TYPES, *STAGE2_MEMBER_TYPES)):
        raise EditorialContextError("context pool has missing, extra or reordered predecessor kinds")
    raw_refs = refs[1:-13]
    if (len(raw_refs) % 2 or any(ref.artifact_type not in ("vlm_request_record", "reprocessed_vlm_evidence")
                               for ref in raw_refs[::2])
            or any(ref.artifact_type != "vlm_semantic_pack" for ref in raw_refs[1::2])):
        raise EditorialContextError("context pool requires every ordered VLM request/semantic pair")
    if (len({(ref.artifact_type, ref.logical_id) for ref in refs}) != len(refs)
            or any(ref.scope != refs[0].scope for ref in refs)):
        raise EditorialContextError("context pool repeats an owner or mixes scopes")
    packs = _raw_packs(pool)
    stage1 = decode_stage1_members(tuple(member.as_artifact_member() for member in pool[-13:-5]), scope=refs[0].scope)
    stage2 = decode_story_design_members(tuple(member.as_artifact_member() for member in pool[-5:]), scope=refs[0].scope)
    _reference_owners(pool, _raw_pool(pool, packs, stage2.business.candidate_catalog.source_grant_sha256))
    if (stage1.admission.next_action != "continue" or stage2.admission.next_action != "continue"
            or stage1.dependency_proof.source_member_ref != refs[0]):
        raise EditorialContextError("context requires complete admitted content bound to this exact Source")
    expected_windows = {window.source_window_ref.object_id for window in stage1.coverage.coverage_ledger.windows}
    if len({pack.window_manifest_sha256 for pack in packs}) != len(packs) or {
        pack.window_manifest_sha256 for pack in packs
    } != expected_windows:
        raise EditorialContextError("context VLM pool does not cover the full admitted Source window universe")
    return stage1, stage2


def _requirements(stage1: Stage1Values, stage2: StoryDesignValues, selection: StorySelection) -> tuple[MaterialEvidenceClosure, ...]:
    proposal = stage2.business.proposal_set.proposals[selection.proposal_index].proposal
    nodes = {node.node_id: node for node in stage1.coverage.narrative_graph.nodes}
    graph_ref = stage1.coverage.identity("narrative_graph")
    rows: list[MaterialEvidenceClosure] = []
    for requirement in proposal.material_requirements:
        node = nodes.get(requirement.obligation_ref.object_id)
        if (requirement.obligation_ref.member_ref != graph_ref or node is None
                or type(node.attributes) is not ObligationAttributes):  # noqa: E721
            raise EditorialContextError("material requirement does not resolve to an exact Graph obligation")
        rows.append(MaterialEvidenceClosure(requirement.requirement_id, requirement.obligation_ref, tuple(
            SemanticObjectRef(graph_ref, "fact", fact) for fact in node.attributes.required_fact_ids
        )))
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class EditorialContextBatch:
    policy: EditorialContextPolicy
    job_policy: JobPolicy
    story_policy: StoryDesignPolicy
    candidate_policy: CandidateCatalogPolicy
    member_pool: tuple[ExactContextMember, ...]
    stories: tuple[StoryEditorialContext, ...]

    def __post_init__(self) -> None:
        if (type(self.policy) is not EditorialContextPolicy or type(self.job_policy) is not JobPolicy  # noqa: E721
                or type(self.story_policy) is not StoryDesignPolicy or type(self.candidate_policy) is not CandidateCatalogPolicy):  # noqa: E721
            raise EditorialContextError("context requires exact explicitly supplied policy values")
        editorial_tuple(self.member_pool, ExactContextMember, nonempty=True)
        editorial_tuple(self.stories, StoryEditorialContext, nonempty=True)
        if len(self.member_pool) > self.policy.max_source_members:
            raise EditorialContextError("complete context exceeds predecessor member bound")
        # A lower bound on the eventual batch bytes: reject before expensive
        # per-Story expansion; full wrappers/policies are checked exactly below.
        if sum(len(member.payload_json.encode("utf-8")) for member in self.member_pool) > self.policy.max_batch_context_bytes:
            raise EditorialContextError("complete predecessor pool exceeds batch byte bound")
        stage1, stage2 = _pool_values(self.member_pool)
        projection = CandidateCatalogProjection(self.member_pool[-5].as_artifact_member(), stage2.business.candidate_catalog)
        if story_design_input_binding(stage1, projection, job_policy=self.job_policy, story_policy=self.story_policy,
                                      candidate_policy=self.candidate_policy) != stage2.admission.input_binding_sha256:
            raise EditorialContextError("context Stage 2 policy/input identities differ from the admitted content")
        if self.target_story_ids != stage2.business.portfolio.target_story_ids:
            raise EditorialContextError("context must preserve the entire frozen target order")
        policies = _policies(self.job_policy, self.story_policy, self.candidate_policy)
        refs = tuple(member.member_ref for member in self.member_pool)
        revisions = {story.closure_member.revision for story in self.stories}
        if len(revisions) != 1:
            raise EditorialContextError("Story contexts must share an output revision")
        for story, selection in zip(self.stories, stage2.business.portfolio.selections, strict=True):
            closure, manifest = story.closure, story.manifest
            if (closure.proposal_ref != selection.proposal_ref or closure.member_refs != refs
                    or closure.requirements != _requirements(stage1, stage2, selection)):
                raise EditorialContextError("Story closure omits or substitutes mandatory predecessor content")
            expanded = _content(self.member_pool, policies, closure)
            if (manifest.context_policy_sha256 != self.policy.canonical_hash
                    or manifest.byte_limit != self.policy.max_story_context_bytes
                    or manifest.context_byte_length != len(expanded)
                    or manifest.context_content_sha256 != sha256_bytes(expanded)):
                raise EditorialContextError("context manifest differs from the complete actual batch projection")
        if len(self.prompt_payload) > self.policy.max_batch_context_bytes:
            raise EditorialContextError("complete deduplicated batch context exceeds byte bound")

    @property
    def target_story_ids(self) -> tuple[str, ...]:
        return tuple(story.story_id for story in self.stories)

    @property
    def input_binding_sha256(self) -> str:
        return canonical_json_hash({"schema_version": "stage3-editorial-input-binding-v1",
                                    "member_refs": [member.member_ref.to_mapping() for member in self.member_pool],
                                    "context_policy_sha256": self.policy.canonical_hash,
                                    "job_policy_sha256": self.job_policy.canonical_hash,
                                    "story_policy_sha256": self.story_policy.canonical_hash,
                                    "candidate_policy_sha256": self.candidate_policy.canonical_hash,
                                    "target_story_ids": list(self.target_story_ids)})

    def to_mapping(self) -> dict[str, object]:
        return {"schema_version": "stage3-editorial-context-batch-v1", "input_binding_sha256": self.input_binding_sha256,
                "context_policy": self.policy.to_mapping(),
                "stage2_policies": _policies(self.job_policy, self.story_policy, self.candidate_policy),
                "member_pool": [member.to_mapping() for member in self.member_pool],
                "stories": [story.to_mapping() for story in self.stories]}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialContextBatch:
        item = editorial_mapping(value, ("schema_version", "input_binding_sha256", "context_policy", "stage2_policies", "member_pool", "stories"))
        policies = editorial_mapping(item["stage2_policies"], ("job_policy", "story_policy", "candidate_policy"))
        if item["schema_version"] != "stage3-editorial-context-batch-v1":
            raise EditorialContextError("unsupported editorial context batch version")
        result = cls(EditorialContextPolicy.from_mapping(item["context_policy"]), JobPolicy.from_mapping(policies["job_policy"]),
                     StoryDesignPolicy.from_mapping(policies["story_policy"]), CandidateCatalogPolicy.from_mapping(policies["candidate_policy"]),
                     editorial_array(item["member_pool"], ExactContextMember.from_mapping),
                     editorial_array(item["stories"], StoryEditorialContext.from_mapping))
        if item["input_binding_sha256"] != result.input_binding_sha256:
            raise EditorialContextError("context batch input binding cannot be self-asserted")
        return result

    @property
    def prompt_payload(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return sha256_bytes(self.prompt_payload)


def _member(kind: str, story_id: str, payload: dict[str, object], scope: ArtifactScope, revision: int) -> ArtifactMember:
    raw = canonical_json_bytes(payload).decode("utf-8")
    return ArtifactMember(kind, f"{kind}@{story_id}", revision, scope, canonical_payload_hash(raw), raw)


def build_editorial_contexts(
    semantic: CommittedSemanticInputs, stage1: Stage1Values, stage2: StoryDesignValues, *,
    policy: EditorialContextPolicy, scope: ArtifactScope, revision: int,
    job_policy: JobPolicy, story_policy: StoryDesignPolicy, candidate_policy: CandidateCatalogPolicy,
) -> EditorialContextBatch:
    """Construct all complete contexts; overflow denies the entire frozen batch.

    Only the upstream committed reader establishes persistence/audit truth. This
    function checks content joins again, reprojects raw candidate evidence and
    retains all supplied payloads, including unselected Proposals and complete
    dependency/state diagnostics. No invented aggregate/response payload fills
    fields absent from CommittedSemanticInputs.
    """
    if (type(semantic) is not CommittedSemanticInputs or type(stage1) is not Stage1Values  # noqa: E721
            or type(stage2) is not StoryDesignValues or type(policy) is not EditorialContextPolicy  # noqa: E721
            or type(scope) is not ArtifactScope):  # noqa: E721
        raise EditorialContextError("context builder requires exact typed inputs and policy")
    editorial_integer(revision, minimum=1)
    if 1 + 2 * len(semantic.inputs) + 13 > policy.max_source_members:
        raise EditorialContextError("complete context exceeds predecessor member bound")
    if (scope != semantic.source_manifest.reference.scope
            or decode_stage1_members(stage1.members, scope=scope) != stage1
            or decode_story_design_members(stage2.members, scope=scope) != stage2):
        raise EditorialContextError("context decoded values differ from their exact predecessor members")
    projection = project_candidate_catalog(semantic, stage1, scope=scope, revision=stage2.members[0].revision, policy=candidate_policy)
    if (projection.member != stage2.members[0] or projection.catalog != stage2.business.candidate_catalog
            or story_design_input_binding(stage1, projection, job_policy=job_policy, story_policy=story_policy,
                                          candidate_policy=candidate_policy) != stage2.admission.input_binding_sha256):
        raise EditorialContextError("context candidates/policies differ from actual raw semantic predecessors")
    source = semantic.source_manifest
    source_ref = source.reference
    members: list[ExactContextMember] = []
    pool_bytes = 0

    def append(member: ArtifactMember) -> None:
        nonlocal pool_bytes
        value = ExactContextMember.from_artifact_member(member)
        pool_bytes += len(value.payload_json.encode("utf-8"))
        if pool_bytes > policy.max_batch_context_bytes:
            raise EditorialContextError("complete predecessor pool exceeds batch byte bound")
        members.append(value)

    append(ArtifactMember(
        source_ref.artifact_type, source_ref.logical_id, source_ref.revision, source_ref.scope, source_ref.content_hash, source.payload_json,
    ))
    for item in semantic.inputs:
        for ref, payload in ((item.semantic_pack.source_child.reference, item.semantic_pack.source_child.payload_json),
                             (item.semantic_pack.reference, item.semantic_pack.payload_json)):
            append(ArtifactMember(
                ref.artifact_type, ref.logical_id, ref.revision, ref.scope, ref.content_hash, payload,
            ))
    for member in (*stage1.members, *stage2.members):
        append(member)
    pool = tuple(members)
    refs = tuple(member.member_ref for member in pool)
    policies = _policies(job_policy, story_policy, candidate_policy)
    stories: list[StoryEditorialContext] = []
    for selection in stage2.business.portfolio.selections:
        closure = EvidenceClosureSet(selection.story_id, selection.proposal_ref, refs, _requirements(stage1, stage2, selection))
        closure_member = _member("evidence_closure_set", selection.story_id, closure.to_mapping(), scope, revision)
        expanded = _content(pool, policies, closure)
        manifest = EditorialContextManifest(selection.story_id, selection.proposal_ref,
                                           SemanticMemberIdentity.from_artifact_member(closure_member), policy.canonical_hash,
                                           sha256_bytes(expanded), len(expanded), policy.max_story_context_bytes)
        context_member = _member("context_manifest", selection.story_id, manifest.to_mapping(), scope, revision)
        stories.append(StoryEditorialContext(selection.story_id, closure_member, context_member))
    return EditorialContextBatch(policy, job_policy, story_policy, candidate_policy, pool, tuple(stories))
