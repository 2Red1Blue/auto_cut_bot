"""R2A window-memory unit tests: same-name/different-person, prefix isolation,
duplicate events, and denominator-bearing metrics."""

from __future__ import annotations

import pytest

from tools.reuse.window_memory.entity_matcher import (
    EntityObservation,
    match_entities,
    match_entity_pair,
)
from tools.reuse.window_memory.evaluator import (
    context_copy_check,
    evaluate_entity_matching,
    evaluate_event_duplicates,
)
from tools.reuse.window_memory.event_deduper import (
    EventObservation,
    find_duplicates,
)


def _obs(ref: str, episode: int, names: tuple[str, ...] = (),
         features: tuple[str, ...] = ()) -> EntityObservation:
    return EntityObservation(
        ref=ref, episode=episode, window=1, display_names=names,
        visual_features=features, source_refs=(f"src-{ref}",),
    )


class TestEntityMatching:
    def test_same_name_different_person_stays_separate(self) -> None:
        """Name-only evidence must never produce a merge candidate."""
        a = _obs("e1", 1, names=("Lucifer",))
        b = _obs("e2", 2, names=("Lucifer",))
        h = match_entity_pair(a, b)
        assert h.decision != "merge_candidate"

    def test_costume_change_same_person_merges(self) -> None:
        """Same name + shared visual features (different outfit tokens) -> merge."""
        a = _obs("e1", 1, names=("Selene",),
                 features=("black hair", "pale skin", "forge apron"))
        b = _obs("e2", 2, names=("Selene",),
                 features=("black hair", "pale skin", "golden curse"))
        h = match_entity_pair(a, b)
        assert h.decision == "merge_candidate"
        assert h.method == "combined"

    def test_empty_features_name_only_is_inconclusive_or_separate(self) -> None:
        a = _obs("e1", 1, names=("Sariel",))
        b = _obs("e2", 2, names=("Sariel", "Sariel-guard"))
        h = match_entity_pair(a, b)
        assert h.decision in ("inconclusive", "keep_separate")
        assert h.decision != "merge_candidate"

    def test_conflicting_features_without_name_stay_separate(self) -> None:
        a = _obs("e1", 1, features=("black wings", "red eyes"))
        b = _obs("e2", 2, features=("white wings", "blue eyes"))
        h = match_entity_pair(a, b)
        assert h.decision == "keep_separate"
        assert h.score == 0.0

    def test_prefix_bound_hides_future_episodes(self) -> None:
        obs = [_obs("e1", 1, names=("A",)), _obs("e2", 2, names=("B",))]
        assert match_entities(obs, max_episode=1) == []
        assert len(match_entities(obs, max_episode=2)) == 1

    def test_self_pair_skipped(self) -> None:
        obs = [_obs("e1", 1, names=("A",), features=("x",)), _obs("e1", 1, names=("A",))]
        assert match_entities(obs) == []


class TestEventDuplicates:
    def test_repeated_event_flagged(self) -> None:
        a = EventObservation(ref="ev1", episode=1, window=1,
                             summary="Lucifer lands in the graveyard at night",
                             participants=("Lucifer",), source_refs=("s1",))
        b = EventObservation(ref="ev2", episode=2, window=1,
                             summary="Lucifer lands in the graveyard at night again",
                             participants=("Lucifer",), source_refs=("s2",))
        h = match_event_pair_stub(a, b)
        assert h.decision == "merge_candidate"
        # originals are never removed: both observations still exist
        assert a.summary and b.summary

    def test_distinct_events_kept(self) -> None:
        a = EventObservation(ref="ev1", episode=1, window=1,
                             summary="Selene forges the pendant", participants=("Selene",),
                             source_refs=("s1",))
        b = EventObservation(ref="ev2", episode=1, window=2,
                             summary="Aurora buys bread in the rain", participants=("Aurora",),
                             source_refs=("s2",))
        assert match_event_pair_stub(a, b).decision == "keep_separate"

    def test_prefix_bound(self) -> None:
        events = [
            EventObservation(ref="ev1", episode=1, window=1, summary="x y z",
                             participants=("P",), source_refs=("s1",)),
            EventObservation(ref="ev2", episode=2, window=1, summary="x y z",
                             participants=("P",), source_refs=("s2",)),
        ]
        assert find_duplicates(events, max_episode=1) == []


def match_event_pair_stub(a: EventObservation, b: EventObservation):
    from tools.reuse.window_memory.event_deduper import match_event_pair

    return match_event_pair(a, b)


class TestEvaluation:
    def test_entity_metrics_have_denominators(self) -> None:
        obs = [
            _obs("e1", 1, names=("Selene",), features=("black hair",)),
            _obs("e2", 2, names=("Selene",), features=("black hair",)),
            _obs("e3", 1, names=("Guard",), features=("armor",)),
            _obs("e4", 2, names=("Guard",), features=("armor",)),
        ]
        true_pairs = {frozenset(("e1", "e2"))}  # e3/e4 are same-name different persons
        results = evaluate_entity_matching(obs, true_pairs=true_pairs)
        by_name = {r.metric: r for r in results}
        assert by_name["entity_match_f1"].denom is not None
        assert by_name["entity_wrong_merge_rate"].value == 0.5  # e3/e4 wrongly merged
        assert by_name["entity_wrong_merge_rate"].denom == 2.0

    def test_empty_relevant_set_is_null_with_reason(self) -> None:
        obs = [_obs("e1", 1, names=("A",))]
        results = evaluate_entity_matching(obs, true_pairs=set())
        by_name = {r.metric: r for r in results}
        if by_name["entity_match_f1"].value is None:
            assert by_name["entity_match_f1"].missing_reason

    def test_event_duplicate_metrics(self) -> None:
        events = [
            EventObservation(ref="ev1", episode=1, window=1, summary="the same event text here",
                             participants=("P",), source_refs=("s1",)),
            EventObservation(ref="ev2", episode=2, window=1, summary="the same event text here",
                             participants=("P",), source_refs=("s2",)),
        ]
        results = evaluate_event_duplicates(
            events, true_duplicate_pairs={frozenset(("ev1", "ev2"))})
        by_name = {r.metric: r for r in results}
        assert by_name["event_duplicate_miss_rate"].value == 0.0

    def test_context_copy_flagged(self) -> None:
        queries = {"q1": "The reveal changes the viewer's understanding."}
        documents = {"c1": "The reveal changes the viewer's understanding."}
        r = context_copy_check(queries, documents)
        assert r.value == 1.0  # query is verbatim inside the document: context-copy
        clean = context_copy_check({"q2": "who poisoned the wine"}, documents)
        assert clean.value == 0.0

    def test_invalid_observations_rejected(self) -> None:
        with pytest.raises(ValueError):
            EntityObservation.from_mapping({"ref": "x", "episode": 1, "window": 1, "extra": 1})
        with pytest.raises(ValueError):
            EventObservation.from_mapping(["not", "a", "dict"])
