"""CLI integration and adversarial tests for the fake protocol experiment."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import tools.reuse.run as run_mod
from tests.reuse.conftest import live_spec, make_spec, write_spec

DEFAULT_EXPERIMENT = "fake-smoke-live"

@dataclass
class Cli:
    repo_root: Path
    fixture: dict[str, str]

    def spec(self, **overrides: Any) -> dict[str, Any]:
        overrides.setdefault("experiment_id", DEFAULT_EXPERIMENT)
        return live_spec(self.fixture, **overrides)

    def replay_spec(self, **overrides: Any) -> dict[str, Any]:
        overrides.setdefault("experiment_id", DEFAULT_EXPERIMENT)
        return make_spec(self.fixture, **overrides)

    def write(self, spec_dict: dict[str, Any]) -> Path:
        path, _ = write_spec(self.repo_root, spec_dict)
        return path

    def run(self, spec_path: Path, *, mode: str, resume: bool = False) -> int:
        return run_mod.main([
            "--spec", str(spec_path),
            "--mode", mode,
            "--media-root", self.fixture["media_root"],
            "--out-root", str(self.repo_root / "artifacts" / "shadow" / "reuse"),
            *(["--resume"] if resume else []),
        ])

    def invoke(self, spec_dict: dict[str, Any], *, mode: str, resume: bool = False) -> int:
        return self.run(self.write(spec_dict), mode=mode, resume=resume)

    def attempts(self, experiment_id: str) -> list[Path]:
        exp = self.repo_root / "artifacts" / "shadow" / "reuse" / experiment_id
        if not exp.is_dir():
            return []
        return sorted(p for p in exp.iterdir() if p.is_dir() and p.name.startswith("attempt-"))

    def metadata(self, attempt: Path) -> dict[str, Any]:
        return json.loads((attempt / "metadata.json").read_text(encoding="utf-8"))

    @property
    def artifacts(self) -> Path:
        return self.repo_root / "artifacts" / "shadow" / "reuse"


@pytest.fixture()
def cli(tmp_repo: Path, fake_fixture: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Cli:
    monkeypatch.setattr(run_mod, "resolve_repo_root", lambda: tmp_repo)
    return Cli(repo_root=tmp_repo, fixture=fake_fixture)


class TestFakeLiveExperiment:
    def test_live_succeeds_with_full_artifacts(self, cli: Cli) -> None:
        assert cli.invoke(cli.spec(), mode="live") == 0
        atts = cli.attempts("fake-smoke-live")
        assert len(atts) == 1
        attempt = atts[0]
        for name in ("metadata.json", "metrics.json", "report.md", ".finalized.json"):
            assert (attempt / name).is_file(), name
        assert (attempt / "projection" / "projection.json").is_file()
        calls = sorted((attempt / "calls").iterdir())
        assert calls, "raw provider calls must be persisted"
        for call_dir in calls:
            assert (call_dir / "request.json").is_file()
            assert (call_dir / "raw_response").is_file()
            result = json.loads((call_dir / "result.json").read_text())
            assert result["status"] == "done"
            assert result["usage"]["input_tokens"] > 0
            assert (call_dir / "request.json").stat().st_size > 0
        meta = cli.metadata(attempt)
        assert meta["status"] == "succeeded"
        assert meta["isolation_passed"] is False  # darwin has no verifiable isolation
        assert "authorization" not in json.dumps(meta).lower()
        # recordings written for replay
        recordings = cli.artifacts / "recordings" / "fake"
        assert any(recordings.iterdir())

    def test_replay_after_live_reuses_recordings(self, cli: Cli) -> None:
        assert cli.invoke(cli.spec(), mode="live") == 0
        # a different spec under the same experiment id is refused: the frozen
        # spec is the experiment's identity, so a mode change needs a new id
        assert cli.invoke(cli.replay_spec(), mode="replay") == 2
        assert len(cli.attempts("fake-smoke-live")) == 1

    def test_recordings_keyed_by_exact_request_hash(self, cli: Cli) -> None:
        """Live run persists recordings keyed by exact request hash for later replay."""
        assert cli.invoke(cli.spec(), mode="live") == 0
        from tools.reuse.providers import RecordingIndex

        recordings = cli.artifacts / "recordings" / "fake"
        index = RecordingIndex(recordings)
        entries = list(recordings.glob("*.json"))
        assert entries
        for entry_path in entries:
            entry = index.get(entry_path.stem)
            assert entry is not None and entry["raw"]

    def test_replay_experiment_hits_live_recordings(self, cli: Cli) -> None:
        """Live records once; a later replay-mode experiment of the same producer
        hits the same request-hash recordings with zero provider calls."""
        assert cli.invoke(cli.spec(experiment_id="rec-src"), mode="live") == 0
        # a replay spec is a different frozen spec, so it needs its own experiment id
        code = cli.invoke(cli.replay_spec(experiment_id="rec-replay"), mode="replay")
        assert code == 0
        atts = cli.attempts("rec-replay")
        assert len(atts) == 1
        meta = cli.metadata(atts[0])
        assert meta["status"] == "succeeded"
        metrics = json.loads((atts[0] / "metrics.json").read_text())
        assert metrics["budget"] is None  # replay spends zero tokens this run

    def test_replay_without_recordings_is_replay_miss(self, cli: Cli) -> None:
        assert cli.invoke(cli.replay_spec(experiment_id="no-rec"), mode="replay") == 1
        atts = cli.attempts("no-rec")
        assert len(atts) == 1
        meta = cli.metadata(atts[0])
        assert meta["status"] == "not_evaluated"
        assert meta["error_code"] == "REPLAY_MISS"

    def test_second_run_appends_and_never_overwrites(self, cli: Cli) -> None:
        spec = cli.spec()
        assert cli.invoke(spec, mode="live") == 0
        first = cli.attempts("fake-smoke-live")[0]
        first_evidence = (first / "calls" / "fake-ep01" / "raw_response").read_text()
        assert cli.invoke(spec, mode="live") == 0
        atts = cli.attempts("fake-smoke-live")
        assert len(atts) == 2
        assert (atts[0] / "calls" / "fake-ep01" / "raw_response").read_text() == first_evidence

    def test_changed_prompt_cannot_reuse_old_recording(self, cli: Cli) -> None:
        assert cli.invoke(cli.spec(), mode="live") == 0
        # same experiment family, different variant => different request hash
        code = cli.invoke(cli.replay_spec(experiment_id="variant-b", variant="candidate"), mode="replay")
        assert code == 1
        meta = cli.metadata(cli.attempts("variant-b")[0])
        assert meta["error_code"] == "REPLAY_MISS"


class TestFakeUnknownResultRecovery:
    def test_unknown_result_resumes_same_attempt_without_recall(
        self, cli: Cli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tools.reuse.providers import FakeProvider

        real_fake = FakeProvider
        state = {"flaky_used": False}

        def flaky_factory(*args: object, **kwargs: object) -> FakeProvider:
            if not state["flaky_used"]:
                state["flaky_used"] = True
                return real_fake(unknown_results={"fake-ep01"})
            return real_fake()

        monkeypatch.setattr(run_mod, "FakeProvider", flaky_factory)

        spec_path = cli.write(cli.spec(experiment_id="recover"))
        assert cli.run(spec_path, mode="live") == 3, "unknown result exits non-zero, attempt stays running"
        atts = cli.attempts("recover")
        assert len(atts) == 1
        meta = cli.metadata(atts[0])
        assert meta["status"] == "running"
        assert meta["pending_call"] == "fake-ep01"
        assert meta["provider_response_id"].startswith("resp-unknown-")

        # resume with a healthy provider: same attempt, poll instead of re-call
        assert cli.run(spec_path, mode="live", resume=True) == 0
        assert len(cli.attempts("recover")) == 1, "recovery must not create a new attempt"
        meta = cli.metadata(cli.attempts("recover")[0])
        assert meta["status"] == "succeeded"
        ep01 = json.loads((atts[0] / "calls" / "fake-ep01" / "result.json").read_text())
        assert ep01["status"] == "done"


class TestAdversarial:
    def test_tampered_media_detected_before_any_run(self, cli: Cli) -> None:
        # fixture verification happens before attempt reservation (doc flow:
        # 解析 spec → 核对 hashes → reserve), so the refusal leaves no attempt
        media_root = Path(cli.fixture["media_root"])
        (media_root / "ep02.mp4").write_bytes(b"tampered")
        assert cli.invoke(cli.spec(experiment_id="tamper2"), mode="live") == 2
        assert cli.attempts("tamper2") == []

    def test_tampered_media_after_freeze(self, cli: Cli) -> None:
        assert cli.invoke(cli.spec(experiment_id="ok"), mode="live") == 0
        media_root = Path(cli.fixture["media_root"])
        (media_root / "ep01.mp4").write_bytes(b"tampered")
        assert cli.invoke(cli.spec(experiment_id="tampered"), mode="live") == 2
        assert cli.attempts("tampered") == []

    def test_wrong_producer_commit_refused(self, cli: Cli) -> None:
        bad = cli.spec(producer_commit="builtin:not-fake")
        assert cli.invoke(bad, mode="live") == 2
        assert not (cli.artifacts / "fake-smoke-live").exists()

    def test_dirty_upstream_detected(self, cli: Cli) -> None:
        repo = cli.repo_root / "upstream" / "DirtyRepo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "--allow-empty", "-qm", "c1"],
            check=True,
        )
        registry = json.loads((cli.repo_root / "tools" / "reuse" / "projects.json").read_text())
        registry["projects"].append({
            "project_id": "Dirty",
            "role": "reference",
            "repo_url": "https://example.com/dirty",
            "local_dir": str(repo),
            "pinned_commit": "0" * 40,  # != actual HEAD
            "license_path": "LICENSE",
            "adapter_version": "d-1",
            "adapter_module": "tools.reuse.adapters.PendingAdapter",
            "status": "registered",
        })
        (cli.repo_root / "tools" / "reuse" / "projects.json").write_text(json.dumps(registry))
        spec = cli.spec(experiment_id="dirty", producer_id="Dirty",
                        producer_commit="0" * 40, adapter_version="d-1")
        assert cli.invoke(spec, mode="live") == 2

    def test_concurrent_run_conflict(self, cli: Cli) -> None:
        spec = cli.spec(experiment_id="race")
        assert cli.invoke(spec, mode="live") == 0
        first = cli.attempts("race")[0]
        meta = cli.metadata(first)
        meta["status"] = "running"
        (first / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        assert cli.invoke(spec, mode="live") == 2

    def test_fixture_manifest_drift_detected(self, cli: Cli, tmp_repo: Path) -> None:
        assert cli.invoke(cli.spec(experiment_id="drift"), mode="live") == 0
        # rewrite the manifest (e.g. label edit) without updating spec hash
        manifest_path = Path(cli.fixture["manifest"])
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        doc["alignment_status"] = "verified" if doc["alignment_status"] != "verified" else "unverified"
        manifest_path.write_text(json.dumps(doc), encoding="utf-8")
        assert cli.invoke(cli.spec(experiment_id="drift2"), mode="live") == 2  # spec hash vs file
        _ = tmp_repo


class TestSpecFreeze:
    def test_second_attempt_with_drifted_spec_refused(self, cli: Cli) -> None:
        spec = cli.spec(experiment_id="frozen")
        assert cli.invoke(spec, mode="live") == 0
        drifted = cli.spec(experiment_id="frozen", seed=7)
        assert cli.invoke(drifted, mode="live") == 2

    def test_mode_mismatch_refused(self, cli: Cli) -> None:
        spec = cli.spec(experiment_id="modes")
        spec_path = cli.write(spec)
        assert cli.run(spec_path, mode="replay") == 2  # CLI replay but spec says live


class TestReviewFixes:
    def test_budget_ledger_rebuilt_on_resume(self, cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
        """After unknown-result recovery the attempt-wide ledger counts ALL calls
        of the attempt (recovered + new), not just the resumed process's calls."""
        from tools.reuse.providers import FakeProvider

        real_fake = FakeProvider
        state = {"flaky_used": False}

        def flaky_factory(*args: object, **kwargs: object) -> FakeProvider:
            if not state["flaky_used"]:
                state["flaky_used"] = True
                return real_fake(unknown_results={"fake-ep01"})
            return real_fake()

        monkeypatch.setattr(run_mod, "FakeProvider", flaky_factory)
        spec_path = cli.write(cli.spec(experiment_id="ledger-resume"))
        assert cli.run(spec_path, mode="live") == 3
        monkeypatch.setattr(run_mod, "FakeProvider", real_fake)
        assert cli.run(spec_path, mode="live", resume=True) == 0
        attempt = cli.attempts("ledger-resume")[0]
        metrics = json.loads((attempt / "metrics.json").read_text())
        assert metrics["budget"]["calls"] == 2  # ep01 (recovered) + ep02 (new)
        assert metrics["budget"]["input_tokens"] == 200

    def test_over_budget_response_keeps_evidence(self, cli: Cli) -> None:
        """A call that trips the token cap fails the attempt but its raw response
        stays persisted — paid tokens always leave evidence."""
        spec = cli.spec(experiment_id="over-budget")
        spec["budget"] = {"max_calls": 10, "max_input_tokens": 150, "max_output_tokens": 10_000, "max_concurrency": 1}
        assert cli.invoke(spec, mode="live") == 1
        atts = cli.attempts("over-budget")
        assert len(atts) == 1
        meta = cli.metadata(atts[0])
        assert meta["status"] == "failed"
        assert meta["error_code"] == "BUDGET_EXHAUSTED"
        saved = sorted(p.name for p in (atts[0] / "calls").iterdir())
        assert saved == ["fake-ep01", "fake-ep02"], "both responses must be persisted"
        assert (atts[0] / "calls" / "fake-ep02" / "raw_response").is_file()

    def test_tampered_recording_is_replay_miss_not_crash(self, cli: Cli) -> None:
        assert cli.invoke(cli.spec(experiment_id="rec-tamper-src"), mode="live") == 0
        recordings = cli.artifacts / "recordings" / "fake"
        target = sorted(recordings.glob("*.json"))[0]
        entry = json.loads(target.read_text())
        entry["request_hash"] = "0" * 64  # internal identity no longer matches filename
        target.write_text(json.dumps(entry), encoding="utf-8")
        assert cli.invoke(cli.replay_spec(experiment_id="rec-tamper"), mode="replay") == 1
        meta = cli.metadata(cli.attempts("rec-tamper")[0])
        assert meta["error_code"] == "REPLAY_MISS"

    def test_run_lock_blocks_concurrent_processes(self, cli: Cli) -> None:
        import fcntl
        import os

        assert cli.invoke(cli.spec(), mode="live") == 0
        lock_path = cli.artifacts / "fake-smoke-live" / ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            spec_path = cli.write(cli.spec())
            code = cli.run(spec_path, mode="live")
            assert code == 2  # another process holds the lock
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_dirty_upstream_detected(self, cli: Cli) -> None:
        repo = cli.repo_root / "upstream" / "DirtyTree"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "f.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "c1"], check=True)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        registry = json.loads((cli.repo_root / "tools" / "reuse" / "projects.json").read_text())
        registry["projects"].append({
            "project_id": "DirtyTree", "role": "reference",
            "repo_url": "https://example.com/dt", "local_dir": str(repo),
            "pinned_commit": head, "license_path": "LICENSE",
            "adapter_version": "d-1", "adapter_module": "tools.reuse.adapters.PendingAdapter",
            "status": "registered",
        })
        (cli.repo_root / "tools" / "reuse" / "projects.json").write_text(json.dumps(registry))
        spec = cli.spec(experiment_id="dt", producer_id="DirtyTree",
                        producer_commit=head, adapter_version="d-1")
        # even a matching HEAD is refused when the working tree is dirty
        (repo / "uncommitted.txt").write_text("y", encoding="utf-8")
        assert cli.invoke(spec, mode="live") == 2

    def test_ipc_model_mismatch_refused(self) -> None:
        from tools.reuse.budget import BudgetLedger
        from tools.reuse.ipc import IpcHost
        from tools.reuse.models import Budget
        from tools.reuse.providers import FakeProvider

        host = IpcHost(
            FakeProvider(), BudgetLedger(Budget(5, 10_000, 10_000, 1)),
            allowed_providers={"fake"}, expected_model="frozen-model",
        )
        from tools.reuse.models import ErrorCode, ExperimentError, ProviderCallRequest

        request = ProviderCallRequest(call_id="c1", provider="fake", model="other-model", payload={})
        with pytest.raises(ExperimentError) as exc:
            host.serve_line(json.dumps(request.to_dict()))
        assert exc.value.code == ErrorCode.SOURCE_MISMATCH

    def test_ipc_host_survives_garbage_input(self) -> None:
        import io

        from tools.reuse.budget import BudgetLedger
        from tools.reuse.ipc import IpcHost
        from tools.reuse.models import Budget
        from tools.reuse.providers import FakeProvider

        host = IpcHost(
            FakeProvider(), BudgetLedger(Budget(5, 10_000, 10_000, 1)), allowed_providers={"fake"}
        )
        reader = io.StringIO("[" * 50000 + "]" * 50000 + "\n")  # RecursionError-style input
        writer = io.StringIO()
        assert host.serve_stream(reader, writer) == 1
        response = json.loads(writer.getvalue())
        assert response["status"] == "error"
