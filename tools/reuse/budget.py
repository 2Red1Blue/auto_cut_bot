"""Call/token budget ledger shared by every provider call of one attempt.

Every call — in-process or arriving over JSONL IPC from an external producer
subprocess — is checked and charged against the same ledger. A subprocess
never holds its own budget state.
"""

from __future__ import annotations

import threading

from tools.reuse.models import Budget, ErrorCode, ExperimentError, Usage


class BudgetLedger:
    """Call/token budget shared by every provider call of one attempt.

    The ledger is in-memory per run but is rebuilt from persisted call evidence
    on --resume, so attempt-level ceilings survive crashes. The R0 executor is
    strictly sequential (parallelism=1, checked against max_concurrency); if a
    later phase introduces parallel calls, check_can_call/charge must be merged
    into a single atomic reservation step to keep the TOCTOU window closed."""

    def __init__(self, budget: Budget) -> None:
        self._budget = budget
        self._lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def check_can_call(self) -> None:
        with self._lock:
            if self.calls >= self._budget.max_calls:
                raise ExperimentError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    f"call budget exhausted: {self.calls}/{self._budget.max_calls}",
                )

    def charge(self, usage: Usage) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            if self.input_tokens > self._budget.max_input_tokens:
                raise ExperimentError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    f"input token budget exceeded: {self.input_tokens}"
                    f">{self._budget.max_input_tokens}",
                )
            if self.output_tokens > self._budget.max_output_tokens:
                raise ExperimentError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    f"output token budget exceeded: {self.output_tokens}"
                    f">{self._budget.max_output_tokens}",
                )

    def to_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "limits": {
                "max_calls": self._budget.max_calls,
                "max_input_tokens": self._budget.max_input_tokens,
                "max_output_tokens": self._budget.max_output_tokens,
                "max_concurrency": self._budget.max_concurrency,
            },
        }

    def validate_concurrency_plan(self, planned_parallelism: int) -> None:
        if planned_parallelism > self._budget.max_concurrency:
            raise ExperimentError(
                ErrorCode.BUDGET_EXHAUSTED,
                f"planned parallelism {planned_parallelism} exceeds "
                f"budget.max_concurrency {self._budget.max_concurrency}",
            )
