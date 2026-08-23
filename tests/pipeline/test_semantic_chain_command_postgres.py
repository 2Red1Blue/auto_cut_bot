"""Opt-in PostgreSQL coverage for the semantic command boundary."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.media.ffprobe_port import ProbeResult
from autocut_kernel.media.preflight import MediaPreflightRequest, preflight
from autocut_kernel.media.types import (
    PTSIndex,
    TimeBase,
    ToolEvidence,
    VideoStreamEvidence,
    canonical_sha256,
)
from autocut_kernel.physical_edit import FixtureBeatInput, SpanSelectionPolicy
from autocut_kernel.pipeline import (
    LocalMediaCommand,
    LocalMediaCommandRequest,
    SemanticChainCommand,
    SemanticChainCommandRequest,
)
from autocut_kernel.pipeline.semantic_chain_command import _set_hash
from autocut_kernel.semantic_chain import (
    CatalogCandidateRef,
    EvidenceRef,
    FactKind,
    RegisteredFact,
    SemanticChainInput,
    SemanticProfile,
)
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandSuccess,
    Job,
    MediaEvidenceReference,
    PostgresRuntimeStore,
)
from test_semantic_chain_command import _Registry, _request

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests"
)


class _FakeRequestStore:
    """Only supplies the helper's in-memory media registration surface."""

    def __init__(self) -> None:
        self.media: dict[tuple[str, str], str] = {}


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class _Port:
    def probe(self, _: Path) -> ProbeResult:
        return ProbeResult(
            VideoStreamEvidence(0, "mpeg4", 64, 48, TimeBase(1, 10)),
            PTSIndex((0, 10, 20, 30)),
            ToolEvidence("fake-ffprobe", "fixture", _hash_bytes(b"")),
        )


class _BridgeBeatResolver:
    def resolve_beat(self, _: object) -> FixtureBeatInput:
        return FixtureBeatInput(0, 10, 20, 30, 10)


def _local_request(
    tmp_path: Path, *, job_key: str = "local-evidence-job"
) -> LocalMediaCommandRequest:
    source_path = tmp_path / "fixture.mp4"
    source_path.write_bytes(b"fixture media bytes")
    source_hash = _hash_bytes(source_path.read_bytes())
    manifest_binding = {
        "fixture_id": "bridge-fixture",
        "profile": "test",
        "schema_version": 1,
        "source": {"content_sha256": source_hash, "byte_size": source_path.stat().st_size},
    }
    sidecar = {
        "fixture_id": "bridge-fixture",
        "profile": "test",
        "schema_version": 1,
        "evidence_mode": "fixture_ground_truth_v1",
        "source": manifest_binding["source"],
        "manifest_hash_binding": {
            "representation": "canonical_manifest_without_sidecar_sha256_v1",
            "sha256": _hash_bytes(
                json.dumps(manifest_binding, sort_keys=True, separators=(",", ":")).encode()
            ),
        },
        "pts_index_sha256": _hash_bytes(b"[0,10,20,30]"),
        "ground_truth": {
            "exact_pts": {
                "representation": "integer_pts_index",
                "time_base": "1/10",
                "values": [0, 10, 20, 30],
            }
        },
        "validity_intervals": [{"start_pts": 0, "end_pts": 30}],
    }
    sidecar_path = tmp_path / "fixture.sidecar.json"
    sidecar_path.write_text(json.dumps(sidecar, sort_keys=True, separators=(",", ":")) + "\n")
    sidecar_hash = _hash_bytes(sidecar_path.read_bytes())
    manifest_path = tmp_path / "fixture.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {**manifest_binding, "sidecar": {"sha256": sidecar_hash}},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    job = Job(job_key, "test")
    return LocalMediaCommandRequest(
        job,
        "local-evidence-v1",
        MediaPreflightRequest(
            "test", source_path, "bridge-fixture", source_hash, manifest_path, sidecar_path
        ),
        FixtureBeatInput(0, 10, 20, 30, 10),
        SpanSelectionPolicy(4),
        ArtifactScope("pipeline", "job", job_key),
    )


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in ("0001_runtime_core.sql", "0002_runtime_core_constraints.sql"):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


def test_postgres_semantic_command_reads_exact_upstream_media_evidence_and_persists_one_set() -> (
    None
):
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    command = SemanticChainCommand(store)
    source = _FakeRequestStore()
    request = _request(source)
    evidence_payload = source.media[
        (request.media_job.job_key, request.media_evidence_reference.content_hash)
    ]
    upstream = store.claim_command(
        CommandClaim(
            request.media_job,
            "upstream-media-evidence-v1",
            "upstream_fixture",
            request.request_hash,
        )
    )
    member = ArtifactMember(
        "media_evidence",
        request.media_evidence_reference.logical_id,
        request.media_evidence_reference.revision,
        request.media_evidence_reference.scope,
        request.media_evidence_reference.content_hash,
        evidence_payload,
    )
    store.commit_command_success(
        CommandSuccess(upstream.command_slot_id, _set_hash((member,)), (member,))
    )
    first = command.execute(request)
    replay = command.execute(request)

    assert first.outcome.state == replay.outcome.state == "succeeded"
    assert first.resolved_beat is not None
    assert replay.outcome.artifact_set_id == first.outcome.artifact_set_id
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone() == (2,)
            cursor.execute("SELECT count(*) FROM runtime.artifacts")
            assert cursor.fetchone() == (4,)


def test_postgres_real_local_media_to_semantic_to_distinct_local_media_job(tmp_path: Path) -> None:
    """Prove the typed bridge crosses the terminal semantic Job boundary."""

    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    port = _Port()
    upstream_request = _local_request(tmp_path)
    upstream = LocalMediaCommand(store, port=port).execute(upstream_request)
    assert upstream.state == "succeeded"

    evidence_result = preflight(upstream_request.preflight_request, port=port)
    assert evidence_result.evidence is not None
    evidence = EvidenceRef("media_evidence", canonical_sha256(evidence_result.evidence.to_json()))
    reference = MediaEvidenceReference(
        upstream_request.artifact_scope, "media_evidence", 1, evidence.content_hash
    )
    candidate = CatalogCandidateRef(
        "candidate_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "catalog_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "sha256:" + "c" * 64,
        evidence,
        SemanticProfile.TEST,
    )
    semantic_input = SemanticChainInput(
        SemanticProfile.TEST,
        (evidence,),
        (
            RegisteredFact(
                "fact_dddddddddddddddddddddddddddddddd",
                FactKind.OBSERVATION,
                evidence,
                candidate,
            ),
        ),
    )
    semantic_job = Job("semantic-bridge-job", "test")
    semantic_request = SemanticChainCommandRequest(
        semantic_job,
        "semantic-bridge-v1",
        semantic_input,
        candidate,
        upstream_request.job,
        reference,
        _Registry(),
        _BridgeBeatResolver(),
        ArtifactScope("pipeline", "job", semantic_job.job_key),
    )
    semantic = SemanticChainCommand(store).execute(semantic_request)
    assert semantic.outcome.state == "succeeded"
    assert semantic.resolved_beat is not None

    downstream_job = Job("downstream-local-media-job", "test")
    downstream_request = replace(
        upstream_request,
        job=downstream_job,
        idempotency_key="downstream-local-media-v1",
        beat=semantic.resolved_beat.beat,
        artifact_scope=ArtifactScope("pipeline", "job", downstream_job.job_key),
    )
    downstream = LocalMediaCommand(store, port=port).execute(downstream_request)

    assert downstream.state == "succeeded"
    assert upstream_request.job != semantic_job != downstream_job
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone() == (3,)
            cursor.execute("SELECT count(*) FROM runtime.artifacts")
            assert cursor.fetchone() == (7,)
