"""Fake producer adapter: the R0 protocol-acceptance adapter.

It exercises the full runner contract — validate, calls, budget, replay/live,
crash recovery, projection, metrics, finalize — without any real provider or
private media. Passing the fake smoke says nothing about real producer
quality; the report explicitly carries that scope note.
"""

from __future__ import annotations

import json
from typing import Any

from tools.reuse.fixtures import FixtureManifest
from tools.reuse.metrics import recall_at_k
from tools.reuse.models import (
    ErrorCode,
    ExperimentError,
    ExperimentSpec,
    MetricResult,
    Projection,
    ProviderCallRequest,
)

ADAPTER_VERSION = "fake-1"
FAKE_COMMIT = "builtin:fake"


class FakeAdapter:
    """One deterministic segment-plan request per fixture input episode."""

    def __init__(self) -> None:
        self._spec: ExperimentSpec | None = None
        self._fixture: FixtureManifest | None = None
        self._accepted: dict[str, str] = {}

    # -- narrow interface ---------------------------------------------------

    def validate(self, spec: ExperimentSpec, fixture: FixtureManifest) -> None:
        if spec.producer_commit != FAKE_COMMIT:
            raise ExperimentError(ErrorCode.SOURCE_MISMATCH, f"fake adapter pins {FAKE_COMMIT}")
        if spec.mode == "live":
            if spec.model_request is None or spec.model_request.provider != "fake":
                raise ExperimentError(ErrorCode.INVALID_SPEC, "live fake experiment requires provider 'fake'")
        if not fixture.inputs:
            raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, "fake adapter needs fixture inputs")

    def build_calls(self, state: dict[str, Any]) -> list[ProviderCallRequest]:
        assert self._spec is not None
        pending = set(state.get("accepted_call_ids", ()))
        calls: list[ProviderCallRequest] = []
        episodes = sorted({item.episode for item in self._fixture.inputs if item.episode is not None})
        if not episodes:
            episodes = [1]
        for episode in episodes:
            call_id = f"fake-ep{episode:02d}"
            if call_id in pending:
                continue
            calls.append(
                ProviderCallRequest(
                    call_id=call_id,
                    provider=self._spec.model_request.provider if self._spec.model_request else "fake",
                    model=self._spec.model_request.model if self._spec.model_request else "fake-model",
                    payload={
                        "task": "fake_segment_plan",
                        "variant": self._spec.variant,
                        "fixture_set": self._fixture.set_id if self._fixture else "",
                        "episode": episode,
                        "seed": self._spec.seed,
                    },
                )
            )
        return calls

    def accept_response(self, call_id: str, raw: str) -> None:
        self._accepted[call_id] = raw

    def finalize(self) -> None:
        assert self._spec is not None
        expected = self.build_calls({"accepted_call_ids": tuple(self._accepted)})
        if expected:
            missing = [c.call_id for c in expected]
            raise ExperimentError(
                ErrorCode.PROJECTION_REJECTED, f"fake adapter incomplete, missing calls: {missing}"
            )

    def project(self, raw_outputs: dict[str, str]) -> Projection:
        items: list[dict[str, Any]] = []
        unmapped: list[dict[str, Any]] = []
        for call_id in sorted(raw_outputs):
            try:
                raw_obj = json.loads(raw_outputs[call_id])
            except json.JSONDecodeError:
                unmapped.append({"call_id": call_id, "reason": "raw is not JSON"})
                continue
            items.append({"call_id": call_id, "fake_segment_plan": raw_obj.get("fake_output")})
        if not items:
            raise ExperimentError(ErrorCode.PROJECTION_REJECTED, "fake projection produced no items")
        return Projection(
            producer_id="fake", projection_version=ADAPTER_VERSION, items=items, unmapped=unmapped
        )

    # -- host hooks ---------------------------------------------------------

    def bind(self, spec: ExperimentSpec, fixture: FixtureManifest) -> None:
        self._spec = spec
        self._fixture = fixture

    def compute_metrics(self, projection: Projection, manifest: FixtureManifest) -> list[MetricResult]:
        """Two denominator-bearing protocol metrics; both null when unmeasurable."""
        results: list[MetricResult] = []
        expected_inputs = len(manifest.inputs)
        covered = sum(
            1
            for item in projection.items
            if item.get("fake_segment_plan") is not None
        )
        if expected_inputs == 0:
            results.append(
                MetricResult("segments_projected", None, missing_reason="no fixture inputs")
            )
        else:
            denom = float(expected_inputs)
            num = float(min(covered, expected_inputs))
            results.append(
                MetricResult(
                    "segments_projected", num / denom if denom else None, num=num, denom=denom
                )
            )
        hit_ids = [item.get("call_id", "") for item in projection.items]
        relevant = {f"fake-ep{e:02d}" for e in {1, 2}}
        results.append(recall_at_k(hit_ids, relevant, k=len(hit_ids) or 1))
        return results
