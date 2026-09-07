"""Budget ledger, FakeProvider determinism and unknown-result recovery."""

from __future__ import annotations

import pytest

from tools.reuse.budget import BudgetLedger
from tools.reuse.models import (
    Budget,
    ErrorCode,
    ExperimentError,
    ProviderCallRequest,
    Usage,
)
from tools.reuse.providers import FakeProvider, RecordingIndex


def _budget(**kw: int) -> Budget:
    base = {"max_calls": 2, "max_input_tokens": 1000, "max_output_tokens": 1000, "max_concurrency": 1}
    base.update(kw)
    return Budget(**base)


def _call(call_id: str = "c1", payload: dict[str, object] | None = None) -> ProviderCallRequest:
    return ProviderCallRequest(call_id=call_id, provider="fake", model="m", payload=payload or {"n": 1})


class TestBudget:
    def test_exhausted_after_max_calls(self) -> None:
        ledger = BudgetLedger(_budget(max_calls=1))
        ledger.check_can_call()
        ledger.charge(Usage(10, 10))
        with pytest.raises(ExperimentError) as exc:
            ledger.check_can_call()
        assert exc.value.code == ErrorCode.BUDGET_EXHAUSTED

    def test_input_token_boundary(self) -> None:
        ledger = BudgetLedger(_budget(max_input_tokens=100))
        ledger.charge(Usage(100, 0))
        assert ledger.input_tokens == 100
        with pytest.raises(ExperimentError) as exc:
            ledger.charge(Usage(1, 0))
        assert exc.value.code == ErrorCode.BUDGET_EXHAUSTED

    def test_output_token_boundary(self) -> None:
        ledger = BudgetLedger(_budget(max_output_tokens=50))
        with pytest.raises(ExperimentError):
            ledger.charge(Usage(0, 51))

    def test_concurrency_plan(self) -> None:
        ledger = BudgetLedger(_budget(max_concurrency=1))
        with pytest.raises(ExperimentError):
            ledger.validate_concurrency_plan(4)
        ledger.validate_concurrency_plan(1)


class TestFakeProvider:
    def test_deterministic_same_request_same_raw(self) -> None:
        a = FakeProvider().call(_call("c1"))
        b = FakeProvider().call(_call("c2"))
        assert a.raw == b.raw
        assert a.status == "done"

    def test_different_payload_different_raw(self) -> None:
        a = FakeProvider().call(_call("c1", {"v": "baseline"}))
        b = FakeProvider().call(_call("c2", {"v": "candidate"}))
        assert a.raw != b.raw

    def test_unknown_then_recover_without_recall(self) -> None:
        provider = FakeProvider(unknown_results={"c1"})
        first = provider.call(_call("c1"))
        assert first.status == "unknown"
        assert first.provider_response_id is not None
        calls_after_unknown = provider.calls_made
        resolved = provider.resolve_unknown("c1", first.provider_response_id)
        assert resolved.status == "done"
        # polling must not count as a new provider call
        assert provider.calls_made == calls_after_unknown


class TestRecordingIndex:
    def test_put_if_absent_and_get(self, tmp_path: object) -> None:
        index = RecordingIndex(tmp_path)  # type: ignore[arg-type]
        assert index.get("abc") is None
        assert index.put_if_absent("abc", "raw", "resp-1", Usage(1, 2)) is True
        assert index.put_if_absent("abc", "other", "resp-2", Usage(3, 4)) is False
        entry = index.get("abc")
        assert entry is not None
        assert entry["raw"] == "raw"
