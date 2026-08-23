"""True Postgres-to-visible-output acceptance coverage for persisted recipes.

Set ``AUTOCUT_TEST_POSTGRES_DSN`` to a disposable database to run this test.
It applies the tracked runtime migrations and uses the controlled fixture corpus,
real FFmpeg/ffprobe, QC, and descriptor-relative promotion.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from autocut_kernel.media.ffprobe_port import FFprobePort
from autocut_kernel.media.preflight import MediaPreflightRequest
from autocut_kernel.physical_edit import FixtureBeatInput, SpanSelectionPolicy
from autocut_kernel.pipeline import (
    LocalMediaCommand,
    LocalMediaCommandRequest,
    PersistedRenderLocalRequest,
    RenderLocalDenied,
    RenderLocalSuccess,
    render_persisted_local,
)
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    Job,
    PostgresRuntimeStore,
    RecipeReference,
)

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests"
)


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in ("0001_runtime_core.sql", "0002_runtime_core_constraints.sql"):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


def _fixture_corpus_module() -> ModuleType:
    """Load the shared test-only fixture corpus without using a legacy pipeline API."""
    path = Path(__file__).parents[1] / "media" / "fixture_corpus.py"
    spec = importlib.util.spec_from_file_location("persisted_recipe_fixture_corpus", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _recipe_reference(
    store: PostgresRuntimeStore, job: Job, scope: ArtifactScope
) -> RecipeReference:
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT content_hash
                  FROM runtime.artifacts
                 WHERE artifact_type = 'recipe'
                   AND namespace = %s AND scope_kind = %s AND scope_key = %s
                """,
                (scope.namespace, scope.kind, scope.key),
            )
            row = cursor.fetchone()
    assert row is not None
    return RecipeReference(scope, "recipe", 1, str(row[0]))


def _set_hash(member: ArtifactMember) -> str:
    payload = [
        {
            "artifact_type": member.artifact_type,
            "content_hash": member.content_hash,
            "logical_id": member.logical_id,
            "payload_json": json.loads(member.payload_json),
            "revision": member.revision,
            "scope": {
                "key": member.scope.key,
                "kind": member.scope.kind,
                "namespace": member.scope.namespace,
            },
        }
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_persisted_recipe_renders_qcs_and_promotes_only_the_exact_reference(tmp_path: Path) -> None:
    assert DSN is not None
    corpus = _fixture_corpus_module()
    registration = corpus.register_fixture_corpus(tmp_path)
    sidecar = json.loads(registration.sidecar_path.read_text())
    pts = sidecar["ground_truth"]["exact_pts"]["values"]
    assert isinstance(pts, list) and len(pts) >= 4 and all(type(value) is int for value in pts)

    job = Job(str(uuid4()), "test")
    scope = ArtifactScope("pipeline", "job", job.job_key)
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    command = LocalMediaCommand(store, port=FFprobePort())
    outcome = command.execute(
        LocalMediaCommandRequest(
            job=job,
            idempotency_key="persisted-render-v1",
            preflight_request=MediaPreflightRequest(
                profile="test",
                source_path=registration.source_path,
                fixture_id=registration.fixture_id,
                expected_source_sha256=registration.source_content_sha256,
                manifest_path=registration.manifest_path,
                sidecar_path=registration.sidecar_path,
            ),
            beat=FixtureBeatInput(pts[0], pts[1], pts[2], pts[3], pts[2] - pts[1]),
            policy=SpanSelectionPolicy(100),
            artifact_scope=scope,
        )
    )
    assert outcome.state == "succeeded"
    reference = _recipe_reference(store, job, scope)

    output_root = tmp_path / "visible-output"
    rendered = render_persisted_local(
        store,
        PersistedRenderLocalRequest(
            job=job,
            recipe_reference=reference,
            source_path=registration.source_path,
            output_root=output_root,
            attempt_id="render-1",
        ),
    )

    assert isinstance(rendered, RenderLocalSuccess)
    current_path = output_root / "results" / job.job_key / "current.json"
    assert current_path.is_file()
    current = json.loads(current_path.read_text())
    manifest_path = output_root / current["manifest"]["path"]
    manifest = json.loads(manifest_path.read_text())
    assert manifest["recipe_provenance"] == {
        "content_hash": reference.content_hash,
        "logical_id": reference.logical_id,
        "revision": reference.revision,
        "scope": {"key": scope.key, "kind": scope.kind, "namespace": scope.namespace},
        "store_job_id": str(store.read_recipe(job, reference).job_id),
        "type": "recipe",
    }

    forged = RecipeReference(
        scope, "recipe", 1, "sha256:" + hashlib.sha256(b"forged recipe reference").hexdigest()
    )
    forged_root = tmp_path / "forged-output"
    denied = render_persisted_local(
        store,
        PersistedRenderLocalRequest(
            job=job,
            recipe_reference=forged,
            source_path=registration.source_path,
            output_root=forged_root,
            attempt_id="render-2",
        ),
    )
    assert isinstance(denied, RenderLocalDenied)
    assert denied.code == "PERSISTED_RECIPE_UNAVAILABLE"
    assert not (forged_root / "results" / job.job_key / "current.json").exists()

    wrong_job = Job(str(uuid4()), "test")
    wrong_job_root = tmp_path / "wrong-job-output"
    wrong_job_denied = render_persisted_local(
        store,
        PersistedRenderLocalRequest(
            job=wrong_job,
            recipe_reference=reference,
            source_path=registration.source_path,
            output_root=wrong_job_root,
            attempt_id="render-3",
        ),
    )
    assert isinstance(wrong_job_denied, RenderLocalDenied)
    # A cross-job reference must be denied without revealing whether the
    # referenced artifact exists for another Job.  Either unavailable or the
    # canonical-scope invariant may be the first fail-closed boundary.
    assert wrong_job_denied.code in {"PERSISTED_RECIPE_UNAVAILABLE", "PERSISTED_RECIPE_INVALID"}
    assert not (wrong_job_root / "results" / wrong_job.job_key / "current.json").exists()

    # A same-Job reference outside the canonical local Recipe scope is refused
    # before Store lookup, rendering, or pointer promotion.
    wrong_scope = ArtifactScope("pipeline", "job", f"{job.job_key}-wrong")
    wrong_scope_reference = RecipeReference(wrong_scope, "recipe", 1, reference.content_hash)
    wrong_scope_root = tmp_path / "wrong-scope-output"
    wrong_scope_denied = render_persisted_local(
        store,
        PersistedRenderLocalRequest(
            job=job,
            recipe_reference=wrong_scope_reference,
            source_path=registration.source_path,
            output_root=wrong_scope_root,
            attempt_id="render-4",
        ),
    )
    assert isinstance(wrong_scope_denied, RenderLocalDenied)
    assert wrong_scope_denied.code == "PERSISTED_RECIPE_INVALID"
    assert not (wrong_scope_root / "results" / job.job_key / "current.json").exists()
