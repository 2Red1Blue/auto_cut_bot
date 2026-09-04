"""Deterministic VLM cross-run reuse *planning*, never reuse authorization.

This module deliberately has no Store, provider, HTTP, or runtime dependency.
It can say which target episode inputs are byte-for-byte compatible with an
explicit base identity and retain the closure that a later privileged Store
operation must independently verify.  It cannot read an origin, grant access
to an origin Blob, claim a command, or certify a reused result.

The full target census is always explicit.  Nodes are emitted only for the
explicit selected set, so a diagnostic selection never silently expands into a
batch execution plan.  A plan is a ``complete_batch`` only when its selection
equals a supplied, identity-matched complete target census; every other
selection is ``inspection``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from ..media.types import canonical_sha256, sha256_prefixed
from .models import VlmValidationError
from .reuse_identity import VlmReuseIdentityV1
from .reuse_identity_v2 import VlmReuseIdentityV2

VLM_REUSE_PLAN_SCHEMA_VERSION = "VlmReusePlan/v1"
VLM_REUSE_PLAN_V2_SCHEMA_VERSION = "VlmReusePlan/v2"
VlmReuseDecision = Literal["reuse", "execute"]
VlmReuseResultScope = Literal["inspection", "complete_batch"]
VlmReuseDecisionReason = Literal[
    "exact_identity_match",
    "missing_base_identity",
    "semantic_policy_mismatch",
    "context_pack_mismatch",
    "input_identity_mismatch",
    "missing_origin_closure",
    "origin_identity_mismatch",
]

_EXECUTE_REASONS = frozenset(
    {
        "missing_base_identity",
        "semantic_policy_mismatch",
        "context_pack_mismatch",
        "input_identity_mismatch",
        "missing_origin_closure",
        "origin_identity_mismatch",
    }
)


def _text(value: object, field_name: str, *, maximum_length: int = 1_024) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum_length:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be explicit non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise VlmValidationError(f"{field_name} must not contain control characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise VlmValidationError(f"{field_name} must be valid UTF-8 text") from error
    return value


def _hash(value: object, field_name: str) -> str:
    if type(value) is not str:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be an explicit sha256 identity")
    return sha256_prefixed(value, field_name)


def _episode_index(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class VlmReuseOriginClosureReference:
    """An untrusted, exact reference set a later Store commit must reread.

    These references do not prove that the origin exists, succeeded, belongs to
    the named job, or remains readable by a target.  Keeping the individual
    immutable member hashes prevents a future binding from replacing a precise
    producer closure with a vague "latest successful run" lookup.
    """

    origin_job_key: str
    origin_profile: str
    origin_child_idempotency_key: str
    origin_attempt_id: str
    origin_reuse_identity_sha256: str
    origin_receipt_sha256: str
    origin_artifact_set_sha256: str
    origin_request_payload_sha256: str
    origin_response_payload_sha256: str
    origin_semantic_pack_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "origin_job_key",
            "origin_profile",
            "origin_child_idempotency_key",
            "origin_attempt_id",
        ):
            _text(getattr(self, field_name), field_name)
        for field_name in (
            "origin_reuse_identity_sha256",
            "origin_receipt_sha256",
            "origin_artifact_set_sha256",
            "origin_request_payload_sha256",
            "origin_response_payload_sha256",
            "origin_semantic_pack_sha256",
        ):
            _hash(getattr(self, field_name), field_name)

    def to_mapping(self) -> dict[str, object]:
        return {
            "origin_artifact_set_sha256": self.origin_artifact_set_sha256,
            "origin_attempt_id": self.origin_attempt_id,
            "origin_child_idempotency_key": self.origin_child_idempotency_key,
            "origin_job_key": self.origin_job_key,
            "origin_profile": self.origin_profile,
            "origin_receipt_sha256": self.origin_receipt_sha256,
            "origin_request_payload_sha256": self.origin_request_payload_sha256,
            "origin_response_payload_sha256": self.origin_response_payload_sha256,
            "origin_reuse_identity_sha256": self.origin_reuse_identity_sha256,
            "origin_semantic_pack_sha256": self.origin_semantic_pack_sha256,
        }


@dataclass(frozen=True, slots=True)
class VlmTargetCensusReference:
    """Independently-bound target SourcePrep census facts.

    A planner cannot establish these facts from a selected VLM subset.  The
    application must obtain them from its already-bound target source census;
    a later privileged Store operation must independently reread that binding.
    This pure value deliberately does not claim that such a binding exists.
    """

    declared_episode_count: int
    target_source_manifest_sha256: str
    target_source_provenance_sha256: str

    def __post_init__(self) -> None:
        if type(self.declared_episode_count) is not int or self.declared_episode_count < 1:  # noqa: E721
            raise VlmValidationError("declared_episode_count must be a positive integer")
        _hash(self.target_source_manifest_sha256, "target_source_manifest_sha256")
        _hash(self.target_source_provenance_sha256, "target_source_provenance_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "declared_episode_count": self.declared_episode_count,
            "target_source_manifest_sha256": self.target_source_manifest_sha256,
            "target_source_provenance_sha256": self.target_source_provenance_sha256,
        }


@dataclass(frozen=True, slots=True)
class VlmReusePlanEpisode:
    """One declared target episode and its explicit, optional base candidate."""

    episode_index: int
    target_identity: VlmReuseIdentityV1 | VlmReuseIdentityV2
    base_identity: VlmReuseIdentityV1 | VlmReuseIdentityV2 | None = None
    origin: VlmReuseOriginClosureReference | None = None

    def __post_init__(self) -> None:
        episode_index = _episode_index(self.episode_index, "episode_index")
        if type(self.target_identity) not in (VlmReuseIdentityV1, VlmReuseIdentityV2):
            raise VlmValidationError("target_identity must be an exact v1 or v2 reuse identity")
        if self.target_identity.episode_index != episode_index:
            raise VlmValidationError("target_identity must belong to episode_index")
        if self.base_identity is not None:
            if type(self.base_identity) is not type(self.target_identity):
                raise VlmValidationError("base_identity and target_identity must use the same version")
            if self.base_identity.episode_index != episode_index:
                raise VlmValidationError("base_identity must belong to episode_index")
        if self.origin is not None:
            if type(self.origin) is not VlmReuseOriginClosureReference:  # noqa: E721
                raise VlmValidationError("origin must be an exact VlmReuseOriginClosureReference")
            if self.base_identity is None:
                raise VlmValidationError("origin requires an explicit base_identity")

    def census_mapping(self) -> dict[str, object]:
        """Canonical, explicit identity facts; origin is node-only evidence."""

        result: dict[str, object] = {
            "base_identity": (
                None if self.base_identity is None else self.base_identity.to_mapping()
            ),
            "base_identity_sha256": (
                None if self.base_identity is None else self.base_identity.canonical_hash
            ),
            "episode_index": self.episode_index,
            "target_identity": self.target_identity.to_mapping(),
            "target_identity_sha256": self.target_identity.canonical_hash,
        }
        if isinstance(self.target_identity, VlmReuseIdentityV2):
            result["target_provenance"] = self.target_identity.provenance_mapping()
            result["base_provenance"] = (
                self.base_identity.provenance_mapping()
                if isinstance(self.base_identity, VlmReuseIdentityV2)
                else None
            )
        return result


def reproject_vlm_reuse_episode_v2(episode: VlmReusePlanEpisode) -> VlmReusePlanEpisode:
    """Explicit pure migration of a reconstructed v1 candidate, without I/O.

This records the original v1 hashes in the v2 census, preserves the origin's
producer/member references and checks its claimed request binding. It does
not prove the origin exists or succeeded: Store must independently reread it
before calling this and again before committing a target-owned reuse result.
No provider dispatch or implicit whole-batch migration is performed.
"""
    if type(episode) is not VlmReusePlanEpisode:  # noqa: E721
        raise VlmValidationError("reprojection requires an exact VlmReusePlanEpisode")
    if type(episode.target_identity) is not VlmReuseIdentityV1:  # noqa: E721
        raise VlmValidationError("reprojection requires v1 target request facts")
    base = episode.base_identity
    origin = episode.origin
    projected_base = None
    if base is not None:
        if type(base) is not VlmReuseIdentityV1:  # noqa: E721
            raise VlmValidationError("reprojection requires v1 base request facts")
        projected_base = VlmReuseIdentityV2(base)
        if origin is not None:
            if (
                origin.origin_reuse_identity_sha256 != base.canonical_hash
                or origin.origin_request_payload_sha256
                != base.origin_request_identity.request_payload_sha256
            ):
                raise VlmValidationError("reprojection origin must bind the original v1 request facts")
            origin = replace(origin, origin_reuse_identity_sha256=projected_base.canonical_hash)
    return VlmReusePlanEpisode(
        episode.episode_index, VlmReuseIdentityV2(episode.target_identity), projected_base, origin
    )


@dataclass(frozen=True, slots=True)
class VlmReusePlanNode:
    """One selected episode's deterministic decision, not a dispatch command."""

    episode_index: int
    decision: VlmReuseDecision
    reason: VlmReuseDecisionReason
    base_identity_sha256: str | None
    target_identity_sha256: str
    origin: VlmReuseOriginClosureReference | None = None

    def __post_init__(self) -> None:
        _episode_index(self.episode_index, "episode_index")
        if self.decision not in ("reuse", "execute"):
            raise VlmValidationError("decision must be 'reuse' or 'execute'")
        _hash(self.target_identity_sha256, "target_identity_sha256")
        if self.base_identity_sha256 is not None:
            _hash(self.base_identity_sha256, "base_identity_sha256")
        if self.decision == "reuse":
            if self.reason != "exact_identity_match":
                raise VlmValidationError("reuse decision requires exact_identity_match")
            if self.base_identity_sha256 is None or self.origin is None:
                raise VlmValidationError("reuse decision requires base identity and origin closure")
            if self.origin.origin_reuse_identity_sha256 != self.base_identity_sha256:
                raise VlmValidationError("origin closure must bind the exact base identity")
        elif self.reason not in _EXECUTE_REASONS:
            raise VlmValidationError("execute decision reason is unsupported")
        elif self.origin is not None:
            raise VlmValidationError("execute decision must not retain an origin closure")

    def to_mapping(self) -> dict[str, object]:
        return {
            "base_identity_sha256": self.base_identity_sha256,
            "decision": self.decision,
            "episode_index": self.episode_index,
            "origin": None if self.origin is None else self.origin.to_mapping(),
            "reason": self.reason,
            "target_identity_sha256": self.target_identity_sha256,
        }


def _decision_for(episode: VlmReusePlanEpisode) -> VlmReusePlanNode:
    """Choose one closed result with deterministic reason precedence.

    Policy changes outrank context changes, and context changes outrank other
    input differences.  This makes the plan explanation stable even if an
    episode has several mismatches at once.
    """

    base = episode.base_identity
    target = episode.target_identity
    if base is None:
        return VlmReusePlanNode(
            episode.episode_index, "execute", "missing_base_identity", None, target.canonical_hash
        )
    if base.semantic_policy.canonical_hash != target.semantic_policy.canonical_hash:
        return VlmReusePlanNode(
            episode.episode_index,
            "execute",
            "semantic_policy_mismatch",
            base.canonical_hash,
            target.canonical_hash,
        )
    if base.context_pack_sha256 != target.context_pack_sha256:
        return VlmReusePlanNode(
            episode.episode_index,
            "execute",
            "context_pack_mismatch",
            base.canonical_hash,
            target.canonical_hash,
        )
    if base.canonical_hash != target.canonical_hash:
        return VlmReusePlanNode(
            episode.episode_index,
            "execute",
            "input_identity_mismatch",
            base.canonical_hash,
            target.canonical_hash,
        )
    if episode.origin is None:
        return VlmReusePlanNode(
            episode.episode_index,
            "execute",
            "missing_origin_closure",
            base.canonical_hash,
            target.canonical_hash,
        )
    if episode.origin.origin_reuse_identity_sha256 != base.canonical_hash:
        return VlmReusePlanNode(
            episode.episode_index,
            "execute",
            "origin_identity_mismatch",
            base.canonical_hash,
            target.canonical_hash,
        )
    return VlmReusePlanNode(
        episode.episode_index,
        "reuse",
        "exact_identity_match",
        base.canonical_hash,
        target.canonical_hash,
        episode.origin,
    )


@dataclass(frozen=True, slots=True)
class VlmReusePlan:
    """Closed pure plan for reuse-or-execute decisions in one target VLM batch.

    The plan's deterministic hash binds the full source census and its target
    census reference, not merely its selected nodes.  It is still not an
    authorization or a successful target result; a privileged later layer must
    independently validate the target census and every reuse closure before it
    can create target-owned evidence.
    """

    target_census: VlmTargetCensusReference
    source_episode_census: tuple[VlmReusePlanEpisode, ...]
    selected_episode_indexes: tuple[int, ...]
    nodes: tuple[VlmReusePlanNode, ...] = field(init=False)
    result_scope: VlmReuseResultScope = field(init=False)

    def __post_init__(self) -> None:
        if type(self.target_census) is not VlmTargetCensusReference:  # noqa: E721
            raise VlmValidationError("target_census must be an exact VlmTargetCensusReference")
        census = tuple(self.source_episode_census)
        selected = tuple(self.selected_episode_indexes)
        if not census:
            raise VlmValidationError("source_episode_census must not be empty")
        if any(type(episode) is not VlmReusePlanEpisode for episode in census):  # noqa: E721
            raise VlmValidationError("source_episode_census must contain exact VlmReusePlanEpisode values")
        if len({type(episode.target_identity) for episode in census}) != 1:
            raise VlmValidationError("source_episode_census must use one explicit identity version")
        expected_indexes = tuple(range(len(census)))
        actual_indexes = tuple(episode.episode_index for episode in census)
        if actual_indexes != expected_indexes:
            raise VlmValidationError("source_episode_census must be ordered contiguous episode indexes")
        if len(census) != self.target_census.declared_episode_count:
            raise VlmValidationError("source_episode_census must match target_census declared episode count")
        if any(
            episode.target_identity.source_manifest_sha256
            != self.target_census.target_source_manifest_sha256
            or episode.target_identity.source_provenance_sha256
            != self.target_census.target_source_provenance_sha256
            for episode in census
        ):
            raise VlmValidationError("source_episode_census target identities must match target_census")
        if not selected:
            raise VlmValidationError("selected_episode_indexes must not be empty")
        if any(type(index) is not int for index in selected):  # noqa: E721
            raise VlmValidationError("selected_episode_indexes must contain integers")
        if selected != tuple(sorted(selected)) or len(selected) != len(set(selected)):
            raise VlmValidationError("selected_episode_indexes must be sorted and unique")
        if any(index not in expected_indexes for index in selected):
            raise VlmValidationError("selected_episode_indexes must be a subset of source_episode_census")
        nodes = tuple(_decision_for(census[index]) for index in selected)
        result_scope: VlmReuseResultScope = (
            "complete_batch" if selected == expected_indexes else "inspection"
        )
        object.__setattr__(self, "source_episode_census", census)
        object.__setattr__(self, "selected_episode_indexes", selected)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "result_scope", result_scope)

    @classmethod
    def build(
        cls,
        *,
        target_census: VlmTargetCensusReference,
        source_episode_census: tuple[VlmReusePlanEpisode, ...],
        selected_episode_indexes: tuple[int, ...],
    ) -> VlmReusePlan:
        """Build a plan without I/O, fallback lookup, selection expansion, or dispatch."""

        return cls(target_census, source_episode_census, selected_episode_indexes)

    def to_mapping(self) -> dict[str, object]:
        return {
            "nodes": [node.to_mapping() for node in self.nodes],
            "result_scope": self.result_scope,
            "schema_version": (
                VLM_REUSE_PLAN_V2_SCHEMA_VERSION
                if isinstance(self.source_episode_census[0].target_identity, VlmReuseIdentityV2)
                else VLM_REUSE_PLAN_SCHEMA_VERSION
            ),
            "selected_episode_indexes": list(self.selected_episode_indexes),
            "source_episode_census": [
                episode.census_mapping() for episode in self.source_episode_census
            ],
            "target_census": self.target_census.to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


__all__ = [
    "VLM_REUSE_PLAN_SCHEMA_VERSION",
    "VLM_REUSE_PLAN_V2_SCHEMA_VERSION",
    "VlmReuseDecision",
    "VlmReuseDecisionReason",
    "VlmReuseOriginClosureReference",
    "VlmReusePlan",
    "VlmReusePlanEpisode",
    "VlmReusePlanNode",
    "VlmReuseResultScope",
    "VlmTargetCensusReference",
    "reproject_vlm_reuse_episode_v2",
]
