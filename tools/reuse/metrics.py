"""Metric protocol: every reported number carries its denominator or a reason.

Metrics are computed from projection + fixture labels only. An empty relevant
set is never 0/0; a missing label source is null + missing_reason. Metric
policy version/revision ride along in metrics.json so scoring semantics can
be audited per attempt.
"""

from __future__ import annotations

from typing import Any

from tools.reuse.models import (
    METRICS_SCHEMA,
    ErrorCode,
    ExperimentError,
    MetricResult,
)


def recall_at_k(hits: list[str], relevant: set[str], k: int, *, group: str | None = None,
                metric: str = "recall_at_k") -> MetricResult:
    """Recall@K over a frozen relevant set. Empty R -> null + reason, never 0."""
    if k <= 0:
        raise ExperimentError(ErrorCode.METRIC_INPUT_INCOMPLETE, "recall_at_k: k must be positive")
    if not relevant:
        return MetricResult(
            metric=metric, value=None, missing_reason="empty relevant set", group=group
        )
    seen: set[str] = set()
    num = 0
    for hit in hits[:k]:
        if hit in relevant and hit not in seen:
            seen.add(hit)
            num += 1
    denom = float(len(relevant))
    return MetricResult(metric=metric, value=num / denom, num=float(num), denom=denom, group=group)


def pair_precision_recall(
    matched_pairs: int, predicted_links: int, true_links: int, *, group: str | None = None
) -> MetricResult:
    """Entity-identity matching precision/recall (same-person pairing)."""
    if predicted_links == 0 and true_links == 0:
        return MetricResult(
            metric="entity_match_f1", value=None, missing_reason="no links predicted or expected", group=group
        )
    precision = matched_pairs / predicted_links if predicted_links else None
    recall = matched_pairs / true_links if true_links else None
    if precision is None or recall is None:
        return MetricResult(
            metric="entity_match_f1",
            value=None,
            num=float(matched_pairs),
            denom=float(max(predicted_links, true_links)),
            missing_reason=f"undefined component: precision={precision}, recall={recall}",
            group=group,
        )
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return MetricResult(
        metric="entity_match_f1", value=f1, num=float(matched_pairs), denom=float(max(predicted_links, true_links)), group=group
    )


def validate_metrics(results: list[MetricResult]) -> None:
    for result in results:
        result.validate()


def metrics_document(
    policy: dict[str, str], results: list[MetricResult], budget: dict[str, Any] | None
) -> dict[str, Any]:
    validate_metrics(results)
    return {
        "schema": METRICS_SCHEMA,
        "metric_policy": policy,
        "results": [r.to_dict() for r in results],
        "budget": budget,
        "note": "values are only meaningful within this experiment's frozen matching rules",
    }
