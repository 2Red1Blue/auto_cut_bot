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


def _evidence() -> EvidenceRef:
    return EvidenceRef("evidence_01", _hash())


def _candidate(evidence: EvidenceRef | None = None) -> CatalogCandidateRef:
    return CatalogCandidateRef("candidate_01", "catalog_01", _hash("c"), evidence or _evidence(), SemanticProfile.TEST)


def test_input_is_frozen_and_canonically_stable() -> None:
    evidence = _evidence()
    first = SemanticChainInput(SemanticProfile.TEST, (evidence,), (RegisteredFact("fact_01", FactKind.OBSERVATION, evidence, _candidate(evidence)),))
    second = SemanticChainInput(SemanticProfile.TEST, (evidence,), (RegisteredFact("fact_01", FactKind.OBSERVATION, evidence, _candidate(evidence)),))
    assert first.canonical_hash == second.canonical_hash
    with pytest.raises(AttributeError):
        first.profile = SemanticProfile.SHADOW  # type: ignore[misc]


def test_input_denies_production_missing_and_foreign_evidence() -> None:
    evidence = _evidence()
    fact = RegisteredFact("fact_01", FactKind.CHANGE, evidence, _candidate(evidence))
    with pytest.raises(ProductionProfileDenied):
        SemanticChainInput(SemanticProfile.PRODUCTION, (evidence,), (fact,))
    with pytest.raises(SemanticChainDenied, match="must not be empty"):
        SemanticChainInput(SemanticProfile.TEST, (), (fact,))
    with pytest.raises(SemanticChainDenied, match="missing from"):
        SemanticChainInput(SemanticProfile.TEST, (evidence,), (RegisteredFact("fact_02", FactKind.CHANGE, EvidenceRef("evidence_02", _hash("b")), _candidate(EvidenceRef("evidence_02", _hash("b")))),))


def test_models_reject_free_text_and_non_hash_evidence() -> None:
    with pytest.raises(SemanticChainDenied):
        EvidenceRef("this is prose", _hash())
    with pytest.raises(SemanticChainDenied):
        EvidenceRef("evidence_01", "not-a-hash")
    with pytest.raises(SemanticChainDenied, match="exact evidence"):
        RegisteredFact("fact_01", FactKind.OBSERVATION, _evidence(), _candidate(EvidenceRef("other", _hash("b"))))
