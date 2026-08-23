from __future__ import annotations

import pytest
from autocut_kernel.semantic_chain import (
    CatalogCandidateRef,
    EvidenceRef,
    FactKind,
    ProductionProfileDenied,
    RegisteredFact,
    SemanticChainDenied,
    SemanticChainInput,
    SemanticProfile,
)


def _hash(digit: str = "a") -> str:
    return f"sha256:{digit * 64}"


def _token(kind: str, digit: str = "a") -> str:
    return f"{kind}_{digit * 32}"


def _evidence() -> EvidenceRef:
    return EvidenceRef(_token("evidence"), _hash())


def _candidate(evidence: EvidenceRef | None = None) -> CatalogCandidateRef:
    return CatalogCandidateRef(_token("candidate"), _token("catalog", "c"), _hash("c"), evidence or _evidence(), SemanticProfile.TEST)


def test_input_is_frozen_and_canonically_stable() -> None:
    evidence = _evidence()
    first = SemanticChainInput(SemanticProfile.TEST, (evidence,), (RegisteredFact(_token("fact"), FactKind.OBSERVATION, evidence, _candidate(evidence)),))
    second = SemanticChainInput(SemanticProfile.TEST, (evidence,), (RegisteredFact(_token("fact"), FactKind.OBSERVATION, evidence, _candidate(evidence)),))
    assert first.canonical_hash == second.canonical_hash
    with pytest.raises(AttributeError):
        first.profile = SemanticProfile.SHADOW  # type: ignore[misc]


def test_input_denies_production_missing_and_foreign_evidence() -> None:
    evidence = _evidence()
    fact = RegisteredFact(_token("fact"), FactKind.CHANGE, evidence, _candidate(evidence))
    with pytest.raises(ProductionProfileDenied):
        SemanticChainInput(SemanticProfile.PRODUCTION, (evidence,), (fact,))
    with pytest.raises(SemanticChainDenied, match="must not be empty"):
        SemanticChainInput(SemanticProfile.TEST, (), (fact,))
    with pytest.raises(SemanticChainDenied, match="missing from"):
        SemanticChainInput(SemanticProfile.TEST, (evidence,), (RegisteredFact(_token("fact", "b"), FactKind.CHANGE, EvidenceRef(_token("evidence", "b"), _hash("b")), _candidate(EvidenceRef(_token("evidence", "b"), _hash("b")))),))


def test_models_reject_free_text_and_non_hash_evidence() -> None:
    with pytest.raises(SemanticChainDenied):
        EvidenceRef("this is prose", _hash())
    with pytest.raises(SemanticChainDenied):
        EvidenceRef(_token("evidence"), "not-a-hash")
    with pytest.raises(SemanticChainDenied, match="exact evidence"):
        RegisteredFact(_token("fact"), FactKind.OBSERVATION, _evidence(), _candidate(EvidenceRef(_token("evidence", "b"), _hash("b"))))


def test_evidence_accepts_the_trusted_local_media_logical_id_only() -> None:
    assert EvidenceRef("media_evidence", _hash()).artifact_id == "media_evidence"
    for unsafe in ("/tmp/clip.mp4", "start_pts=42", "12.5_seconds", "score=0.99"):
        with pytest.raises(SemanticChainDenied, match="media_evidence"):
            EvidenceRef(unsafe, _hash())


@pytest.mark.parametrize("payload", ("candidate_12.5_seconds", "/tmp/clip.mp4", "start_pts=42", "score=0.99"))
def test_boundary_tokens_reject_timing_path_and_score_payloads(payload: str) -> None:
    with pytest.raises(SemanticChainDenied, match="opaque token"):
        CatalogCandidateRef(payload, _token("catalog"), _hash("c"), _evidence(), SemanticProfile.TEST)
