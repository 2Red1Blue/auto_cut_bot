"""Unit tests for Phase 1+2 knowledge chain optimizations."""
from __future__ import annotations
import sys
import json
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from autocut_core.libs.artifact_validator import (
    _levenshtein_distance,
    _fuzzy_match_id,
    fixup_fuzzy_ids_in_value,
    check_refs,
)
from autocut_core.semantic.prep.chapters import (
    _build_short_id_map,
    _format_event_dsl,
    _compute_chapter_boundaries,
)
from autocut_core.semantic.prep.global_segmenter import _validate_chapter_boundaries
from autocut_core.semantic.prep.two_pass_chapter import (
    merge_chapter_results,
    update_rolling_context,
)


def test_levenshtein_distance():
    """Test edit distance calculation."""
    assert _levenshtein_distance("kitten", "sitting") == 3
    assert _levenshtein_distance("event-f51a0a378ac1", "event-f51a0a378ac") == 1
    assert _levenshtein_distance("event-a1b2c3d4e5f6", "event-a1b2c3d4e5f5") == 1
    assert _levenshtein_distance("same", "same") == 0
    assert _levenshtein_distance("short", "longerstring") == 9
    print("✅ test_levenshtein_distance passed")


def test_fuzzy_match_id():
    """Test fuzzy ID matching logic."""
    known = {"event-f51a0a378ac1", "event-1234567890ab", "char-lucifer", "thread-revenge"}

    # Single character truncation (most common error)
    match = _fuzzy_match_id("event-f51a0a378ac", known)
    assert match == "event-f51a0a378ac1"

    # Single character typo
    match = _fuzzy_match_id("event-f51a0a378ac2", known)
    assert match == "event-f51a0a378ac1"

    # Missing character in middle
    match = _fuzzy_match_id("event-123456789ab", known)  # missing '0'
    assert match == "event-1234567890ab"

    # Ambiguous match (multiple candidates) returns None
    known2 = {"event-aaaaa", "event-aaaab", "event-aaaac"}
    match = _fuzzy_match_id("event-aaaa", known2)
    assert match is None

    # Distance > 1 returns None
    match = _fuzzy_match_id("event-xxx", known)
    assert match is None

    # Non-ID prefixes don't match
    match = _fuzzy_match_id("randomstring", known)
    assert match is None

    # Character ID typo
    match = _fuzzy_match_id("char-lucife", known)
    assert match == "char-lucifer"
    print("✅ test_fuzzy_match_id passed")


def test_fixup_fuzzy_ids_in_value():
    """Test recursive ID fixup in nested structures."""
    known = {"event-f51a0a378ac1", "event-1234567890ab", "char-lucifer"}

    test_data = {
        "evidence_event_ids": ["event-f51a0a378ac", "event-1234567890ab", "event-badid"],
        "character": {
            "id": "char-lucife",
            "name": "Lucifer"
        },
        "nested": {
            "list": ["event-f51a0a378a", "event-1234567890ab"]
        }
    }
    fixes = []
    fixed = fixup_fuzzy_ids_in_value(test_data, known, fixes)
    assert fixed == 2  # 2 typos fixed
    assert test_data["evidence_event_ids"][0] == "event-f51a0a378ac1"
    assert test_data["character"]["id"] == "char-lucifer"
    assert len(fixes) == 2
    print("✅ test_fixup_fuzzy_ids_in_value passed")


def test_check_refs_auto_fix():
    """Test check_refs with auto-fix."""
    known = {"event-f51a0a378ac1", "event-1234567890ab"}
    errors = []
    fixes = []
    values = ["event-f51a0a378ac", "event-1234567890ab"]
    fixed_count = check_refs(values, known, "test.field", errors, fixes)
    assert fixed_count == 1
    assert values[0] == "event-f51a0a378ac1"
    assert len(errors) == 0  # Error was fixed
    print("✅ test_check_refs_auto_fix passed")


def test_short_id_map_and_dsl():
    """Test short ID mapping and DSL event formatting."""
    test_events = [
        {"id": "event-aaa", "episode": 1, "function": "setup", "character_names": ["Lucifer", "Selene"],
         "summary": "Lucifer returns to hell", "cause": "After rebellion", "effect": "Becomes king",
         "time_range": {"start": 10.0, "end": 20.0}},
        {"id": "event-bbb", "episode": 1, "function": "escalation", "character_names": ["Lucifer"],
         "summary": "Demons bow to Lucifer", "cause": "", "effect": "", "time_range": {"start": 20.0, "end": 30.0}},
        {"id": "event-ccc", "episode": 2, "function": "reveal", "character_names": ["Aurora", "Lucifer"],
         "summary": "Lucifer finds his daughter", "open_question": "Will they recognize each other?",
         "time_range": {"start": 100.0, "end": 120.0}},
    ]
    short_to_full, full_to_short = _build_short_id_map(test_events)
    assert len(short_to_full) == 3
    assert short_to_full["E01"] == "event-aaa"
    assert short_to_full["E02"] == "event-bbb"
    assert short_to_full["E03"] == "event-ccc"
    assert full_to_short["event-aaa"] == "E01"

    # Test DSL formatting
    dsl = _format_event_dsl("E01", test_events[0])
    assert "[E01|EP1|setup]" in dsl
    assert "Lucifer" in dsl
    assert "因After rebellion" in dsl
    assert "-> Becomes king" in dsl
    assert len(dsl) < 200  # Much shorter than JSON equivalent
    print("✅ test_short_id_map_and_dsl passed")


def test_chapter_boundary_validation():
    """Test chapter boundary validation."""
    # Valid boundaries
    valid_chapters = [
        {"start_ep": 1, "end_ep": 5, "title": "Ch1"},
        {"start_ep": 6, "end_ep": 11, "title": "Ch2"},
        {"start_ep": 12, "end_ep": 17, "title": "Ch3"},
    ]
    assert _validate_chapter_boundaries(valid_chapters, 17) is True

    # Too short chapter
    invalid_short = [{"start_ep": 1, "end_ep": 2, "title": "Too short"}]
    assert _validate_chapter_boundaries(invalid_short, 2) is False

    # Gap between chapters
    has_gap = [
        {"start_ep": 1, "end_ep": 5, "title": "Ch1"},
        {"start_ep": 7, "end_ep": 11, "title": "Ch2"},  # Gap at EP6
    ]
    assert _validate_chapter_boundaries(has_gap, 11) is False

    # Overlapping chapters
    overlapping = [
        {"start_ep": 1, "end_ep": 6, "title": "Ch1"},
        {"start_ep": 5, "end_ep": 10, "title": "Ch2"},  # Overlap at EP5
    ]
    assert _validate_chapter_boundaries(overlapping, 10) is False

    # Too long chapter
    too_long = [{"start_ep": 1, "end_ep": 15, "title": "Too long"}]
    assert _validate_chapter_boundaries(too_long, 15) is False
    print("✅ test_chapter_boundary_validation passed")


def test_merge_chapter_results():
    """Test merging two-pass results into standard chapter format."""
    short_id_map = {"E01": "event-aaa", "E02": "event-bbb", "E03": "event-ccc"}
    all_event_ids = {"event-aaa", "event-bbb", "event-ccc"}
    pass1 = {
        "summary": "Lucifer becomes king of hell and finds his daughter.",
        "story_thread_updates": [
            {"thread_id": "T01", "title": "Lucifer's reign", "status": "introduced",
             "summary": "Lucifer takes the throne", "event_eids": ["E01", "E02"]},
        ],
        "new_facts": ["Lucifer is the demon king"],
        "new_open_questions": ["Will Aurora recognize her father?"],
    }
    pass2 = {
        "character_rollup": [
            {"character_key": "char-lucifer", "name": "Lucifer", "aliases": ["Satan"],
             "state_at_start": "Fallen angel", "state_at_end": "King of Hell",
             "evidence_eids": ["E01", "E02", "E03"]},
            {"character_key": "char-aurora", "name": "Aurora", "aliases": [],
             "state_at_start": "Street performer", "state_at_end": "Found by Lucifer",
             "evidence_eids": ["E03"]},
        ],
        "relationship_rollup": [
            {"relationship_key": "rel-lucifer-aurora", "character_key_a": "char-lucifer",
             "character_key_b": "char-aurora", "summary": "Father and daughter, reunited",
             "evidence_eids": ["E03"]},
        ]
    }
    result = merge_chapter_results(
        chapter_id="chapter-001-002",
        episodes=[1, 2],
        pass1_result=pass1,
        pass2_result=pass2,
        short_id_map=short_id_map,
        chapter_meta={"title": "The Return", "arc_type": "setup"},
        all_event_ids=all_event_ids,
    )
    assert result["chapter_id"] == "chapter-001-002"
    assert result["episodes"] == [1, 2]
    assert len(result["character_rollup"]) == 2
    assert len(result["story_threads"]) == 1
    assert result["story_threads"][0]["event_ids"] == ["event-aaa", "event-bbb"]
    assert "event-ccc" in result["character_rollup"][0]["evidence_event_ids"]
    print("✅ test_merge_chapter_results passed")


def test_rolling_context_update():
    """Test rolling context updates across chapters."""
    rolling = {"characters": [], "relationships": [], "threads": []}
    chapter1 = {
        "character_rollup": [
            {"character_key": "char-lucifer", "name": "Lucifer", "aliases": ["Satan"],
             "state_at_end": "King of Hell"},
        ],
        "relationship_rollup": [],
        "story_threads": [
            {"thread_key": "thread-revenge", "title": "Revenge on heaven",
             "summary": "Lucifer plans revenge", "status": "introduced"},
        ]
    }
    rolling = update_rolling_context(rolling, chapter1)
    assert len(rolling["characters"]) == 1
    assert rolling["characters"][0]["id"] == "char-lucifer"
    assert len(rolling["threads"]) == 1

    # Next chapter adds new character and updates existing one
    chapter2 = {
        "character_rollup": [
            {"character_key": "char-lucifer", "name": "Lucifer", "aliases": [],
             "state_at_end": "Finds Aurora"},  # Updates existing
            {"character_key": "char-aurora", "name": "Aurora", "aliases": [],
             "state_at_end": "Meets Lucifer"},  # New
        ],
        "relationship_rollup": [
            {"relationship_key": "rel-lucifer-aurora", "character_key_a": "char-lucifer",
             "character_key_b": "char-aurora", "summary": "Father and daughter"},
        ],
        "story_threads": [
            {"thread_key": "thread-revenge", "title": "Revenge on heaven",
             "summary": "Revenge is delayed by reunion", "status": "advanced"},  # Updates existing
        ]
    }
    rolling = update_rolling_context(rolling, chapter2)
    assert len(rolling["characters"]) == 2
    assert rolling["characters"][0]["state_at_end"] == "Finds Aurora"
    assert len(rolling["relationships"]) == 1
    assert rolling["threads"][0]["status"] == "advanced"
    print("✅ test_rolling_context_update passed")


def test_dynamic_chaptering():
    """Test heuristic dynamic chaptering with overlap and tail merging."""
    # Create 13 test episodes
    test_eps = []
    for i in range(13):
        ep = {
            "episode": i + 1,
            "ending_state": "解决问题" if i in (4, 10) else "",  # End of arc at EP5 and EP11
            "opening_state": "新的开始" if i in (5, 11) else "",  # New arc at EP6 and EP12
            "character_mentions": {"a": 1, "b": 2} if i < 5 else ({"c":1,"d":2} if i < 11 else {"e":1,"f":2}),
        }
        test_eps.append(ep)
    boundaries = _compute_chapter_boundaries(test_eps, target_size=6, overlap=1)
    # Should produce 2 full chapters plus tail merged, total ~2 chapters with overlap
    assert len(boundaries) >= 2
    # Check no chapter is shorter than 3 episodes
    for start, end, cid in boundaries:
        length = end - start
        assert length >= 3, f"Chapter {cid} too short: {length} episodes"
        assert length <= 7, f"Chapter {cid} too long: {length} episodes"
    print("✅ test_dynamic_chaptering passed")


if __name__ == "__main__":
    test_levenshtein_distance()
    test_fuzzy_match_id()
    test_fixup_fuzzy_ids_in_value()
    test_check_refs_auto_fix()
    test_short_id_map_and_dsl()
    test_chapter_boundary_validation()
    test_merge_chapter_results()
    test_rolling_context_update()
    test_dynamic_chaptering()
    print("\n🎉 All Phase 1+2 unit tests passed!")
