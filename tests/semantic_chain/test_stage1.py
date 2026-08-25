from __future__ import annotations

import json
from uuid import uuid4

import pytest
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.semantic_chain import (
    AuditedInputDisposition,
    AuditedStage1Draft,
    CompilerObligation,
    CoverageLedger,
    CoverageRow,
    EventCard,
    FrozenStage1Policy,
    Stage1AuthorityError,
    Stage1CompilationError,
    compile_stage1,
)
from autocut_kernel.source_manifest import (
    DecodedSeriesSource,
    SourceOperationGrant,
    SourceOperationPolicy,
)
from autocut_kernel.store import (
    ArtifactScope,
    BlobRef,
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
    Job,
    PersistedVlmSemanticPack,
    PersistedWholeSeriesSourceManifest,
    SourceWindowIdentity,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.vlm import decode_vlm_semantic_pack

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _semantic_pack():
    # This fixture uses the production VLM parser payload and real closed decoder.
    from tests.vlm.test_observation_decoder import _mapping

    return decode_vlm_semantic_pack(_mapping())


def _committed_inputs(*, purpose: bool = True) -> CommittedSemanticInputs:
    job = Job("stage1-test", "shadow")
    blob = BlobRef(uuid4(), HASH_A, 1, "video/mp4")
    payload_json = json.dumps({"source": "committed"})
    manifest = PersistedWholeSeriesSourceManifest(
        WholeSeriesSourceManifestReference(
            ArtifactScope("pipeline", "job", job.job_key),
            "whole_series_source_manifest",
            1,
            canonical_sha256({"source": "committed"}),
        ),
        payload_json,
        (blob,),
        uuid4(), uuid4(), uuid4(), uuid4(), job,
    )
    purposes = ("semantic_analysis",) if purpose else ("render_source",)
    grant = SourceOperationGrant(
        SourceOperationPolicy("stage1-authority", "stage1-series", 1, purposes),
        "all_or_nothing",
        (DecodedSeriesSource("episode-001.mp4", "episode-001", HASH_A, 1),),
    )
    source_window = SourceWindowIdentity(0, 0, 0, 10, HASH_B, "episode-001", HASH_A, "source-clock", HASH_C, blob)
    persisted_pack = object.__new__(PersistedVlmSemanticPack)
    object.__setattr__(persisted_pack, "semantic_pack", _semantic_pack())
    request_identity = object.__new__(__import__("autocut_kernel.vlm", fromlist=["VlmRequestIdentity"]).VlmRequestIdentity)
    response_record = object.__new__(__import__("autocut_kernel.store", fromlist=["CommittedArtifactMemberReference"]).CommittedArtifactMemberReference)
    return CommittedSemanticInputs(
        manifest,
        grant,
        (CommittedVlmSemanticInput(source_window, request_identity, persisted_pack, response_record, blob),),
    )


def _draft(inputs: CommittedSemanticInputs, status: str = "resolved") -> AuditedStage1Draft:
    return AuditedStage1Draft(
        HASH_C,
        (AuditedInputDisposition(inputs.inputs[0].source_window.window_manifest_sha256, status),),  # type: ignore[arg-type]
        (CompilerObligation("semantic_closure"),),
    )


def _policy() -> FrozenStage1Policy:
    return FrozenStage1Policy("strict-global-v1", HASH_A)


def test_stage1_is_deterministic_and_semantic_only() -> None:
    inputs = _committed_inputs()
    first = compile_stage1(inputs, _draft(inputs), _policy())
    second = compile_stage1(inputs, _draft(inputs), _policy())

    assert first.canonical_hash == second.canonical_hash
    assert first.coverage_admission.to_mapping()["decision"] == "admitted"
    rendered = json.dumps(first.to_mapping()).casefold()
    assert all(token not in rendered for token in ("highlight", "asr", "vad", "pts", "cut_endpoint"))


@pytest.mark.parametrize("status", ["tainted", "unresolved", "conflicted"])
def test_stage1_denies_non_resolved_committed_inputs(status: str) -> None:
    inputs = _committed_inputs()

    result = compile_stage1(inputs, _draft(inputs, status), _policy())

    assert result.coverage_admission.to_mapping()["decision"] == "denied"
    assert result.evidence_diagnostics.to_mapping()["non_resolved_window_manifest_sha256s"]


def test_stage1_denies_missing_semantic_analysis_authorization() -> None:
    inputs = _committed_inputs(purpose=False)

    result = compile_stage1(inputs, _draft(inputs), _policy())

    assert result.coverage_admission.to_mapping()["decision"] == "denied"
    assert result.coverage_admission.to_mapping()["rules"][0]["status"] == "fail"


def test_stage1_rejects_missing_or_duplicate_coverage_universe_rows() -> None:
    with pytest.raises(Stage1CompilationError, match="sorted and unique"):
        CoverageLedger((CoverageRow("fact:a", "fact", "resolved"), CoverageRow("fact:a", "fact", "resolved")))
    with pytest.raises(Stage1AuthorityError, match="input_dispositions"):
        AuditedStage1Draft(HASH_C, (), (CompilerObligation("semantic_closure"),))


def test_stage1_rejects_physical_or_unsupported_event_card_fields() -> None:
    with pytest.raises(TypeError):
        EventCard(HASH_A, "state_change", (HASH_B,), HASH_C, "semantic fact", pts=1)  # type: ignore[call-arg]
