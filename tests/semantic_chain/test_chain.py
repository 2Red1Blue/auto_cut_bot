from __future__ import annotations

import ast
from pathlib import Path

import pytest
from autocut_kernel.semantic_chain import (
    EvidenceRef,
    FactKind,
    RegisteredFact,
    SemanticChainBuilder,
    SemanticChainDenied,
    SemanticChainInput,
    SemanticProfile,
)


def _hash(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _source() -> SemanticChainInput:
    first = EvidenceRef("evidence_01", _hash("a"))
    second = EvidenceRef("evidence_02", _hash("b"))
    return SemanticChainInput(SemanticProfile.TEST, (second, first), (RegisteredFact("fact_02", FactKind.CHANGE, second), RegisteredFact("fact_01", FactKind.OBSERVATION, first)))


def test_builder_is_deterministic_and_profile_bound() -> None:
    builder = SemanticChainBuilder()
    first = builder.build(_source())
    second = builder.build(_source())
    assert first.canonical_hash == second.canonical_hash
    assert first.profile is SemanticProfile.TEST
    assert [event.node_id for event in first.story.events] == [node.node_id for node in first.narrative.nodes]
    assert first.story.narrative_hash == first.narrative.canonical_hash
    assert first.blueprint.story_hash == first.story.canonical_hash


def test_builder_denies_wrong_type_and_foreign_stage_input() -> None:
    builder = SemanticChainBuilder()
    with pytest.raises(SemanticChainDenied):
        builder.build({})  # type: ignore[arg-type]
    source = _source()
    narrative = builder.build_narrative(source)
    foreign = SemanticChainInput(SemanticProfile.SHADOW, source.evidence, source.facts)
    with pytest.raises(SemanticChainDenied, match="exact narrative"):
        builder.build_story(foreign, narrative)


def test_semantic_modules_import_no_runtime_or_legacy_dependencies() -> None:
    root = Path(__file__).parents[2] / "packages" / "autocut-kernel" / "src" / "autocut_kernel" / "semantic_chain"
    forbidden = {"store", "subprocess", "autocut_core", "provider", "llm", "openai"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import) for _ in [0]]
        imports += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(part in forbidden for name in imports for part in name.split("."))
