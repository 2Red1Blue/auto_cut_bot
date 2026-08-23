"""Opt-in acceptance coverage for the cut_bot runtime adapter boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
from autocut_kernel.agent_runtime import (
    AgentRunStage,
    AgentRunState,
    AgentRuntimeService,
    LocalOutputConfiguration,
)
from autocut_kernel.media.ffprobe_port import FFprobePort
from autocut_kernel.physical_edit import FixtureBeatInput, SpanSelectionPolicy
from autocut_kernel.pipeline import (
    LocalMediaCommand,
    LocalRenderOrchestrator,
    ResolutionPolicyIdentity,
    SemanticChainCommand,
)
from autocut_kernel.scenario_registry import FixtureScenarioRegistry, ScenarioRef
from autocut_kernel.scenario_registry.registry import _FixtureScenarioRegistration
from autocut_kernel.semantic_chain import CatalogResolution, FactKind, SemanticProfile
from autocut_kernel.store import PostgresRuntimeStore

from auto_cut_bot.autocut_agent_runtime import AgentRuntimeAdapter, AgentRuntimeRequest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN or shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="set AUTOCUT_TEST_POSTGRES_DSN and install ffmpeg/ffprobe for adapter acceptance",
)


def _fixture_corpus_module() -> ModuleType:
    path = Path(__file__).parents[1] / "media" / "fixture_corpus.py"
    spec = importlib.util.spec_from_file_location("cut_bot_adapter_fixture_corpus", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _retag_fixture(registration: object) -> tuple[Path, Path, Path, str]:
    """Adapt the test corpus to the registry's closed composition fixture ID."""

    source_path = registration.source_path
    fixture_id = "fixture_" + "a" * 32
    manifest_path = registration.manifest_path
    sidecar_path = registration.sidecar_path
    manifest = json.loads(manifest_path.read_text())
    sidecar = json.loads(sidecar_path.read_text())
    binding = {
        "fixture_id": fixture_id,
        "profile": "test",
        "schema_version": manifest["schema_version"],
        "source": manifest["source"],
    }
    sidecar["fixture_id"] = fixture_id
    sidecar["manifest_hash_binding"]["sha256"] = _sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    )
    sidecar_path.write_text(json.dumps(sidecar, sort_keys=True, separators=(",", ":")) + "\n")
    manifest_path.write_text(
        json.dumps(
            {**binding, "sidecar": {"sha256": _sha256(sidecar_path.read_bytes())}},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return source_path, manifest_path, sidecar_path, fixture_id


class _Catalog:
    def resolve_exact(self, candidate, _):
        return CatalogResolution(candidate, candidate.evidence)


class _BeatResolver:
    def __init__(self, beat: FixtureBeatInput) -> None:
        self._beat = beat

    def resolve_beat(self, _):
        return self._beat


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in ("0001_runtime_core.sql", "0002_runtime_core_constraints.sql"):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


def test_adapter_projects_real_kernel_runtime_to_safe_current_output(tmp_path: Path) -> None:
    """The cut_bot edge sees only opaque trace identity and the promoted path."""

    assert DSN is not None
    corpus = _fixture_corpus_module()
    registration = corpus.register_fixture_corpus(tmp_path)
    source_path, manifest_path, sidecar_path, fixture_id = _retag_fixture(registration)
    pts = json.loads(sidecar_path.read_text())["ground_truth"]["exact_pts"]["values"]
    beat = FixtureBeatInput(pts[0], pts[1], pts[2], pts[3], pts[2] - pts[1])
    scenario = ScenarioRef("scenario_" + "b" * 32)
    registry = FixtureScenarioRegistry._from_composition(
        (
            _FixtureScenarioRegistration(
                scenario,
                SemanticProfile.TEST,
                source_path,
                fixture_id,
                registration.source_content_sha256,
                manifest_path,
                sidecar_path,
                beat,
                SpanSelectionPolicy(100),
                "candidate_" + "c" * 32,
                "catalog_" + "d" * 32,
                "sha256:" + "e" * 64,
                "fact_" + "f" * 32,
                FactKind.OBSERVATION,
                _Catalog(),
                _BeatResolver(beat),
                ResolutionPolicyIdentity("resolution_policy_" + "1" * 32, "sha256:" + "2" * 64),
            ),
        )
    )
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    kernel_runtime = AgentRuntimeService(
        registry,
        LocalMediaCommand(store, port=FFprobePort()),
        SemanticChainCommand(store),
        LocalMediaCommand(store, port=FFprobePort()),
        store,
        LocalRenderOrchestrator(store),
        LocalOutputConfiguration(tmp_path / "visible-output"),
    )

    response = AgentRuntimeAdapter(kernel_runtime).run(
        AgentRuntimeRequest("agent_run_" + "3" * 32, SemanticProfile.TEST, scenario)
    )

    assert response.state is AgentRunState.SUCCEEDED
    assert [(stage.stage, stage.state) for stage in response.stages] == [
        (AgentRunStage.UPSTREAM_MEDIA, "succeeded"),
        (AgentRunStage.SEMANTIC, "succeeded"),
        (AgentRunStage.DOWNSTREAM_MEDIA, "succeeded"),
        (AgentRunStage.RENDER, "succeeded"),
    ]
    assert response.output_path is not None and response.output_path.is_file()
    assert response.output_path.name == "current.json"
    assert not hasattr(response, "recipe") and not hasattr(response, "pts")
