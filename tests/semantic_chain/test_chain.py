from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.semantic_chain import (
    CatalogCandidateRef,
    EditorialBlueprint,
    EvidenceRef,
    FactKind,
    RegisteredFact,
    SemanticChain,
    SemanticChainBuilder,
    SemanticChainDenied,
    SemanticChainInput,
    SemanticProfile,
)


def _hash(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _token(kind: str, digit: str) -> str:
    return f"{kind}_{digit * 32}"


def _source() -> SemanticChainInput:
    first = EvidenceRef(_token("evidence", "a"), _hash("a"))
    second = EvidenceRef(_token("evidence", "b"), _hash("b"))
    return SemanticChainInput(
        SemanticProfile.TEST,
        (second, first),
        (
            RegisteredFact(_token("fact", "b"), FactKind.CHANGE, second, CatalogCandidateRef(_token("candidate", "b"), _token("catalog", "c"), _hash("c"), second, SemanticProfile.TEST)),
            RegisteredFact(_token("fact", "a"), FactKind.OBSERVATION, first, CatalogCandidateRef(_token("candidate", "a"), _token("catalog", "c"), _hash("c"), first, SemanticProfile.TEST)),
        ),
    )


def test_builder_is_deterministic_and_profile_bound() -> None:
    builder = SemanticChainBuilder()
    first = builder.build(_source())
    second = builder.build(_source())
    assert first.canonical_hash == second.canonical_hash
    assert first.profile is SemanticProfile.TEST
    assert [event.node_id for event in first.story.events] == [node.node_id for node in first.narrative.nodes]
    assert first.story.narrative_hash == first.narrative.canonical_hash
    assert first.blueprint.story_hash == first.story.canonical_hash
    assert [event.candidate for event in first.story.events] == [node.candidate for node in first.narrative.nodes]
    assert [beat.candidate for beat in first.blueprint.beats] == [event.candidate for event in first.story.events]


def test_builder_denies_wrong_type_and_foreign_stage_input() -> None:
    builder = SemanticChainBuilder()
    with pytest.raises(SemanticChainDenied):
        builder.build({})  # type: ignore[arg-type]
    source = _source()
    narrative = builder.build_narrative(source)
    foreign = SemanticChainInput(
        SemanticProfile.SHADOW,
        source.evidence,
        tuple(replace(fact, candidate=replace(fact.candidate, profile=SemanticProfile.SHADOW)) for fact in source.facts),
    )
    with pytest.raises(SemanticChainDenied, match="exact narrative"):
        builder.build_story(foreign, narrative)


def test_chain_denies_blueprint_with_hash_matched_but_substituted_candidate() -> None:
    chain = SemanticChainBuilder().build(_source())
    original = chain.blueprint.beats[0]
    substituted = replace(
        original,
        candidate=CatalogCandidateRef(
            _token("candidate", "f"),
            original.candidate.catalog_source_id,
            original.candidate.catalog_source_hash,
            original.evidence,
            SemanticProfile.TEST,
        ),
    )
    blueprint = EditorialBlueprint(
        chain.blueprint.blueprint_id,
        chain.story.canonical_hash,
        SemanticProfile.TEST,
        (substituted, *chain.blueprint.beats[1:]),
    )
    with pytest.raises(SemanticChainDenied, match="exact deterministic expansion"):
        SemanticChain(chain.narrative, chain.story, blueprint)


def test_semantic_modules_import_no_runtime_or_legacy_dependencies() -> None:
    root = Path(__file__).parents[2] / "packages" / "autocut-kernel" / "src" / "autocut_kernel" / "semantic_chain"
    forbidden = {"store", "subprocess", "autocut_core", "provider", "llm", "openai"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(part in forbidden for name in imports for part in name.split("."))


def test_import_guard_inspects_every_alias_in_a_single_import_statement() -> None:
    tree = ast.parse("import harmless, autocut_kernel.media.types")
    imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert "autocut_kernel.media.types" in imports
    assert any(part == "media" for name in imports for part in name.split("."))
