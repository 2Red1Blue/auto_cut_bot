"""Unit tests for attempt storage: append-only, atomic finalize, recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.reuse.models import (
    AttemptStatus,
    ErrorCode,
    ExperimentError,
    ProviderCallRequest,
    ProviderResult,
    Usage,
    json_sha256,
)
from tools.reuse.storage import ExperimentStore, write_atomic

BASE_META = {"experiment_id": "exp", "mode": "replay"}


def _make_call(call_id: str = "call-1") -> ProviderCallRequest:
    return ProviderCallRequest(call_id=call_id, provider="fake", model="m", payload={"n": 1})


def _done_result(call: ProviderCallRequest) -> ProviderResult:
    return ProviderResult(raw=f"raw-{call.call_id}", provider_response_id="resp-x", usage=Usage(10, 20), status="done")


class TestReserve:
    def test_sequential_attempts_are_appended(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        a1, meta1 = store.reserve(exp, "hash", BASE_META, resume=False)
        store.mark(a1, status=AttemptStatus.FAILED, error_code="BUDGET_EXHAUSTED")
        a2, meta2 = store.reserve(exp, "hash", BASE_META, resume=False)
        assert (a1.name, a2.name) == ("attempt-0000", "attempt-0001")
        assert meta2["attempt_id"] == "attempt-0001"

    def test_refuses_while_attempt_running(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        a1, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        store.mark(a1, status=AttemptStatus.RUNNING)
        with pytest.raises(ExperimentError) as exc:
            store.reserve(exp, "hash", BASE_META, resume=False)
        assert exc.value.code == ErrorCode.ATTEMPT_CONFLICT

    def test_resume_reuses_non_terminal_attempt(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        a1, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        store.mark(a1, status=AttemptStatus.RUNNING)
        a2, _ = store.reserve(exp, "hash", BASE_META, resume=True)
        assert a1 == a2

    def test_resume_refuses_different_spec_hash(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        a1, _ = store.reserve(exp, "hash-1", BASE_META, resume=False)
        store.mark(a1, status=AttemptStatus.RUNNING)
        with pytest.raises(ExperimentError) as exc:
            store.reserve(exp, "hash-2", BASE_META, resume=True)
        assert exc.value.code == ErrorCode.SPEC_HASH_MISMATCH

    def test_resume_requires_resumable_attempt(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        a1, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        store.mark(a1, status=AttemptStatus.FAILED)
        with pytest.raises(ExperimentError):
            store.reserve(exp, "hash", BASE_META, resume=True)

    def test_unsafe_experiment_id_rejected(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        with pytest.raises(ExperimentError) as exc:
            store.experiment_dir("../escape")
        assert exc.value.code == ErrorCode.PATH_ESCAPE


class TestSpecIdentity:
    def test_ensure_spec_detects_drift(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        store.ensure_spec(exp, json_sha256({"a": 1}), {"a": 1})
        with pytest.raises(ExperimentError) as exc:
            store.ensure_spec(exp, json_sha256({"a": 2}), {"a": 2})
        assert exc.value.code == ErrorCode.SPEC_HASH_MISMATCH


class TestCallsAndFinalize:
    def _fill_attempt(self, store: ExperimentStore, attempt: Path) -> None:
        call = _make_call()
        store.save_call_request(attempt, call)
        store.save_call_result(attempt, call, _done_result(call))

    def test_finalize_requires_all_artifacts(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        attempt, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        self._fill_attempt(store, attempt)
        with pytest.raises(ExperimentError):
            store.finalize(attempt, {"projection": "p", "metrics": "m", "report": "r"})
        store.write_projection(attempt, {"items": []})
        store.write_metrics(attempt, {"results": []})
        store.write_report(attempt, "# report")
        store.finalize(attempt, {"projection": "p", "metrics": "m", "report": "r"})
        assert store.is_finalized(attempt)
        meta = store.load_metadata(attempt)
        assert meta["status"] == AttemptStatus.SUCCEEDED.value

    def test_finalize_refuses_unknown_call(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        attempt, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        call = _make_call()
        store.save_call_request(attempt, call)
        store.save_call_result(
            attempt, call,
            ProviderResult(raw="", provider_response_id="resp-u", usage=Usage(1, 1), status="unknown"),
        )
        store.write_projection(attempt, {"items": []})
        store.write_metrics(attempt, {"results": []})
        store.write_report(attempt, "# report")
        with pytest.raises(ExperimentError) as exc:
            store.finalize(attempt, {"projection": "p", "metrics": "m", "report": "r"})
        assert exc.value.code == ErrorCode.PROVIDER_RESULT_UNKNOWN

    def test_duplicate_call_request_is_idempotent_for_same_request(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        attempt, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        call = _make_call()
        store.save_call_request(attempt, call)
        # rewriting the same deterministic request repairs partial writes
        store.save_call_request(attempt, call)
        # a different request under the same call id is refused
        other = ProviderCallRequest(call_id="call-1", provider="fake", model="m", payload={"n": 2})
        with pytest.raises(ExperimentError) as exc:
            store.save_call_request(attempt, other)
        assert exc.value.code == ErrorCode.ATTEMPT_CONFLICT

    def test_orphan_attempt_dir_does_not_deadlock(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        # crash window: attempt dir created but metadata.json never written
        orphan = exp / "attempt-0000"
        orphan.mkdir(parents=True)
        attempt, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        assert attempt.name == "attempt-0001"
        store.mark(attempt, status=AttemptStatus.FAILED)
        a3, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        assert a3.name == "attempt-0002"

    def test_resume_refuses_finalized_attempt(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        a1, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        call = _make_call()
        store.save_call_request(a1, call)
        store.save_call_result(a1, call, _done_result(call))
        store.write_projection(a1, {"items": []})
        store.write_metrics(a1, {"results": []})
        store.write_report(a1, "# report")
        store.finalize(a1, {"projection": "p", "metrics": "m", "report": "r"})
        with pytest.raises(ExperimentError) as exc:
            store.reserve(exp, "hash", BASE_META, resume=True)
        assert exc.value.code == ErrorCode.ATTEMPT_CONFLICT

    def test_completed_calls_refuse_tampered_raw(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        attempt, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        call = _make_call()
        store.save_call_request(attempt, call)
        store.save_call_result(attempt, call, _done_result(call))
        (attempt / "calls" / "call-1" / "raw_response").write_text("tampered", encoding="utf-8")
        with pytest.raises(ExperimentError) as exc:
            store.load_completed_calls(attempt)
        assert exc.value.code == ErrorCode.PROJECTION_REJECTED

    def test_completed_calls_rebuild_after_crash(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        attempt, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        call = _make_call()
        store.save_call_request(attempt, call)
        store.save_call_result(attempt, call, _done_result(call))
        state = store.load_completed_calls(attempt)
        assert state["call-1"]["raw"] == "raw-call-1"
        assert state["call-1"]["result"]["status"] == "done"

    def test_write_atomic_never_leaves_tmp(self, tmp_path: Path) -> None:
        target = tmp_path / "x" / "y.json"
        write_atomic(target, "{}")
        assert target.is_file()
        assert not (tmp_path / "x" / "y.json.tmp").exists()

    def test_metadata_json_is_strict(self, tmp_path: Path) -> None:
        store = ExperimentStore(tmp_path)
        exp = store.experiment_dir("exp")
        attempt, _ = store.reserve(exp, "hash", BASE_META, resume=False)
        meta_path = attempt / "metadata.json"
        meta_path.write_text('{"status": "reserved", "status": "running"}', encoding="utf-8")
        with pytest.raises(ExperimentError):
            store.load_metadata(attempt)
