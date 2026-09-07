"""R2A evaluation: entity-pair P/R, wrong-merge rate, duplicate/miss counts.

Every metric carries a denominator or a null + reason (tools.reuse.metrics
protocol). Labels come from video-verified annotations; hypotheses are only
suggestions, so evaluation never mutates the underlying observations.
"""

from __future__ import annotations

from typing import Any

from tools.reuse.metrics import pair_precision_recall, recall_at_k
from tools.reuse.models import MetricResult
from tools.reuse.window_memory.entity_matcher import (
    EntityMatchHypothesis,
    EntityObservation,
    match_entities,
)
from tools.reuse.window_memory.event_deduper import (
    EventDuplicateHypothesis,
    EventObservation,
    find_duplicates,
)


def evaluate_entity_matching(
    observations: list[EntityObservation],
    *,
    # video-verified labels: frozensets of ref-pairs that ARE the same person
    true_pairs: set[frozenset[str]],
    max_episode: int | None = None,
) -> list[MetricResult]:
    """Pair P/R and wrong-merge rate for merge_candidate hypotheses."""
    hypotheses = [
        h for h in match_entities(observations, max_episode=max_episode)
        if h.decision == "merge_candidate"
    ]
    predicted = {frozenset((h.left_ref, h.right_ref)) for h in hypotheses}
    matched = len(predicted & true_pairs)
    results = [
        pair_precision_recall(matched, len(predicted), len(true_pairs))
    ]
    wrong_merges = len(predicted - true_pairs)
    results.append(MetricResult(
        metric="entity_wrong_merge_rate",
        value=(wrong_merges / len(predicted)) if predicted else None,
        num=float(wrong_merges),
        denom=float(len(predicted)) if predicted else None,
        missing_reason=None if predicted else "no merge candidates predicted",
    ))
    return results


def evaluate_event_duplicates(
    events: list[EventObservation],
    *,
    true_duplicate_pairs: set[frozenset[str]],
    max_episode: int | None = None,
) -> list[MetricResult]:
    hypotheses = [
        h for h in find_duplicates(events, max_episode=max_episode)
        if h.decision == "merge_candidate"
    ]
    predicted = {frozenset((h.left_ref, h.right_ref)) for h in hypotheses}
    matched = len(predicted & true_duplicate_pairs)
    results = [
        pair_precision_recall(matched, len(predicted), len(true_duplicate_pairs))
    ]
    missed = len(true_duplicate_pairs - predicted)
    denom = float(len(true_duplicate_pairs))
    results.append(MetricResult(
        metric="event_duplicate_miss_rate",
        value=missed / denom if denom else None,
        num=float(missed), denom=denom,
        missing_reason=None if denom else "no labeled duplicate pairs",
    ))
    return results


def evaluate_retrieval(
    hits_by_query: dict[str, list[str]],
    relevant_by_query: dict[str, set[str]],
    k: int,
) -> list[MetricResult]:
    """Recall@K per query (grouped) plus the micro-average across queries."""
    results: list[MetricResult] = []
    num_sum = 0.0
    denom_sum = 0.0
    for query_id in sorted(relevant_by_query):
        relevant = relevant_by_query[query_id]
        hits = hits_by_query.get(query_id, [])
        r = recall_at_k(hits, relevant, k, group=query_id, metric=f"recall_at_{k}")
        results.append(r)
        if r.value is not None and r.denom is not None:
            num_sum += r.num or 0.0
            denom_sum += r.denom
    if denom_sum == 0:
        results.append(MetricResult(
            metric=f"recall_at_{k}_micro", value=None,
            missing_reason="no query had a non-empty relevant set",
        ))
    else:
        results.append(MetricResult(
            metric=f"recall_at_{k}_micro", value=num_sum / denom_sum,
            num=num_sum, denom=denom_sum,
        ))
    return results


def context_copy_check(
    queries: dict[str, str], documents: dict[str, str]
) -> MetricResult:
    """Queries whose text already appears verbatim in an indexed document are
    context-copy — they must never count as independent understanding."""
    flagged = [
        qid for qid, qtext in queries.items()
        if any(qtext.strip() and qtext in dtext for dtext in documents.values())
    ]
    total = float(len(queries))
    if total == 0:
        return MetricResult("context_copy_rate", None, missing_reason="no queries")
    return MetricResult(
        "context_copy_rate", len(flagged) / total,
        num=float(len(flagged)), denom=total,
    )


def hypothesis_diagnostics(
    hypotheses: list[EntityMatchHypothesis] | list[EventDuplicateHypothesis],
) -> dict[str, Any]:
    decisions: dict[str, int] = {}
    for h in hypotheses:
        decisions[h.decision] = decisions.get(h.decision, 0) + 1
    return {"total": len(hypotheses), "decisions": decisions}
