from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from autocut_kernel.media.ffprobe_port import ProbeResult
from autocut_kernel.media.preflight import MediaPreflightRequest
from autocut_kernel.media.types import PTSIndex, TimeBase, ToolEvidence, VideoStreamEvidence
from autocut_kernel.physical_edit import FixtureBeatInput, SpanSelectionPolicy
from autocut_kernel.pipeline import LocalMediaCommand, LocalMediaCommandRequest
from autocut_kernel.store import (
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
)


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: dict[str, object]) -> str:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return _hash_bytes(raw)


class _Port:
    def __init__(self) -> None:
        self.calls = 0

    def probe(self, _: Path) -> ProbeResult:
        self.calls += 1
        return ProbeResult(
            video_stream=VideoStreamEvidence(0, "mpeg4", 64, 48, TimeBase(1, 10)),
            pts_index=PTSIndex((0, 10, 20, 30)),
            tool=ToolEvidence("fake-ffprobe", "fake-ffprobe 1", _hash_bytes(b"")),
        )


class _Store:
    def __init__(self) -> None:
        self.claims: list[CommandClaim] = []
        self.successes: list[CommandSuccess] = []
        self.rejections: list[CommandRejection] = []
        self._outcomes: dict[tuple[str, str], CommandOutcome] = {}

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        self.claims.append(claim)
        key = (claim.job.job_key, claim.idempotency_key)
        if key in self._outcomes:
            return self._outcomes[key]
        outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True)
        self._outcomes[key] = outcome
        return outcome

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        return self._replace(success.command_slot_id, "succeeded", artifact_set=True)

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        return self._replace(
            rejection.command_slot_id,
            rejection.outcome,
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
        )

    def _replace(
        self,
        slot_id: object,
        state: str,
        *,
        artifact_set: bool = False,
        failure_code: str | None = None,
        failure_detail_json: str | None = None,
    ) -> CommandOutcome:
        for key, current in self._outcomes.items():
            if current.command_slot_id == slot_id:
                outcome = CommandOutcome(
                    current.command_slot_id,
                    state,  # type: ignore[arg-type]
                    artifact_set_id=uuid4() if artifact_set else None,
                    failure_code=failure_code,
                    failure_detail_json=failure_detail_json,
                )
                self._outcomes[key] = outcome
                return outcome
        raise AssertionError("unknown command slot")


def _request(tmp_path: Path, *, profile: str = "test", minimum_duration_pts: int = 10) -> LocalMediaCommandRequest:
    source_path = tmp_path / "fixture.mp4"
    source_path.write_bytes(b"fixture media bytes")
    source_hash = _hash_bytes(source_path.read_bytes())
    manifest_binding = {
        "fixture_id": "local-command-fixture",
        "profile": profile,
        "schema_version": 1,
        "source": {"content_sha256": source_hash, "byte_size": source_path.stat().st_size},
    }
    sidecar = {
        "fixture_id": "local-command-fixture",
        "profile": profile,
        "schema_version": 1,
        "evidence_mode": "fixture_ground_truth_v1",
        "source": {"content_sha256": source_hash, "byte_size": source_path.stat().st_size},
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
    sidecar_hash = _write_json(sidecar_path, sidecar)
    manifest_path = tmp_path / "fixture.manifest.json"
    _write_json(
        manifest_path,
        {**manifest_binding, "sidecar": {"sha256": sidecar_hash}},
    )
    return LocalMediaCommandRequest(
        job=Job("local-command-job", profile),  # type: ignore[arg-type]
        idempotency_key="local-media-v1",
        preflight_request=MediaPreflightRequest(
            profile=profile,  # type: ignore[arg-type]
            source_path=source_path,
            fixture_id="local-command-fixture",
            expected_source_sha256=source_hash,
            manifest_path=manifest_path,
            sidecar_path=sidecar_path,
        ),
        beat=FixtureBeatInput(0, 10, 20, 30, minimum_duration_pts),
        policy=SpanSelectionPolicy(4),
        artifact_scope=ArtifactScope("pipeline", "job", "local-command-job"),
    )


def test_success_persists_exactly_evidence_and_recipe_and_replay_does_not_probe(tmp_path: Path) -> None:
    store = _Store()
    port = _Port()
    command = LocalMediaCommand(store, port=port)  # type: ignore[arg-type]
    request = _request(tmp_path)

    first = command.execute(request)
    replay = command.execute(request)

    assert first.state == "succeeded"
    assert replay.state == "succeeded"
    assert port.calls == 1
    assert len(store.successes) == 1
    artifacts = store.successes[0].artifacts
    assert [item.artifact_type for item in artifacts] == ["media_evidence", "recipe"]
    recipe = json.loads(artifacts[1].payload_json)
    assert recipe["source"] == recipe["evidence"]["source"]
    assert recipe["selection_key"] == [0, 0, 10, 10, 20]
    assert recipe["timebase"] == {"denominator": 10, "numerator": 1}
    assert recipe["span"] == {"end_pts": 20, "start_pts": 10}


def test_no_legal_span_is_a_denial_without_artifacts(tmp_path: Path) -> None:
    store = _Store()
    command = LocalMediaCommand(store, port=_Port())  # type: ignore[arg-type]

    outcome = command.execute(_request(tmp_path, minimum_duration_pts=31))

    assert outcome.state == "denied"
    assert outcome.artifact_set_id is None
    assert not store.successes
    assert store.rejections[0].failure_code == "NO_LEGAL_SPAN"
    assert json.loads(store.rejections[0].failure_detail_json)["code"] == "NO_LEGAL_SPAN"


def test_production_fixture_is_denied_before_the_port_is_called(tmp_path: Path) -> None:
    store = _Store()
    port = _Port()
    command = LocalMediaCommand(store, port=port)  # type: ignore[arg-type]

    outcome = command.execute(_request(tmp_path, profile="production"))

    assert outcome.state == "denied"
    assert outcome.failure_code == "TEST_FIXTURE_PROFILE_FORBIDDEN"
    assert port.calls == 0
    assert not store.successes
