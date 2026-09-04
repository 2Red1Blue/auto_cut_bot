"""Portable semantic identity, with source ownership retained as provenance.

This is a pure projection, not an authorization or a successful-result reader.
Store callers must reconstruct the request facts from the exact persisted
origin, not load a hash-only v1 mapping or infer current provider defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..media.types import canonical_sha256
from .models import VlmValidationError
from .reuse_identity import (
    VlmReuseIdentityV1,
    VlmReuseRequestFacts,
    VlmSemanticPolicyIdentityV1,
)


@dataclass(frozen=True, slots=True)
class VlmReuseIdentityV2:
    """Compare ``canonical_hash`` for compatibility, not dataclass equality.

The facts retain the complete original payload and owner, so two compatible
instances can (and usually will) have different provenance and equality.
Window/proxy object identities are deliberately preserved; this version fixes
cross-Job source binding, not arbitrary media relocation/re-identification.
"""

    facts: VlmReuseIdentityV1

    def __post_init__(self) -> None:
        if type(self.facts) is not VlmReuseIdentityV1:  # noqa: E721
            raise VlmValidationError("v2 projection requires exact validated v1 request facts")
        # Re-run the existing closed payload/binding checks; never trust a
        # deserialized projection as evidence that those checks happened.
        object.__setattr__(self, "facts", replace(self.facts))

    @classmethod
    def from_request(
        cls,
        request: VlmReuseRequestFacts,
        *,
        semantic_policy: VlmSemanticPolicyIdentityV1,
    ) -> VlmReuseIdentityV2:
        return cls(VlmReuseIdentityV1.from_request(request, semantic_policy=semantic_policy))

    @property
    def semantic_policy(self) -> VlmSemanticPolicyIdentityV1:
        return self.facts.semantic_policy

    @property
    def context_pack_sha256(self) -> str | None:
        return self.facts.context_pack_sha256

    @property
    def episode_index(self) -> int:
        return self.facts.episode_index

    @property
    def source_manifest_sha256(self) -> str:
        return self.facts.source_manifest_sha256

    @property
    def source_provenance_sha256(self) -> str:
        return self.facts.source_provenance_sha256

    def to_mapping(self) -> dict[str, object]:
        result = self.facts.to_mapping()
        result["kind"] = "VlmReuseIdentity/v2"
        # Only owner/full SourcePrep hashes move out of compatibility. Every
        # remaining v1 fact, including future additions, stays bound.
        del result["source_provenance_sha256"]
        del result["source_manifest_sha256"]
        return result

    def provenance_mapping(self) -> dict[str, object]:
        return {
            "projection": "validated-request-facts-v1-to-v2",
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_provenance_sha256": self.source_provenance_sha256,
            "v1_identity_sha256": self.facts.canonical_hash,
            "request_payload_sha256": self.facts.origin_request_identity.request_payload_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
