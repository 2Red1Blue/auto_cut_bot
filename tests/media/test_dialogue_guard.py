"""Leaf-operation compatibility tests for dialogue protection."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media import MediaKind
from autocut_kernel.physical_edit.dialogue_guard import (
    DialogueGuardError,
    group_transcript_words,
    merge_speech_activity,
    roll_protected_audio_ranges,
)

from tests.media.test_root_evidence import _context, _speech, _transcript


def test_candidate_reusable_leaf_operations_preserve_grouping_merging_and_root_clamp() -> None:
    audio_context = _context(MediaKind.AUDIO, "root-audio-boundaries")
    transcript = _transcript(replace(audio_context, producer_id="sensevoice"))
    speech = _speech(replace(audio_context, producer_id="fsmn-vad"))

    utterances = group_transcript_words(transcript, audio_context, word_gap_tick=7)
    vad_ranges = merge_speech_activity(speech, audio_context, vad_merge_gap_tick=2)
    protected = roll_protected_audio_ranges(
        utterances + vad_ranges,
        audio_context,
        pre_roll_tick=20,
        post_roll_tick=100,
    )

    assert utterances == ((10, 20),)
    assert vad_ranges == ((8, 38),)
    assert [(item.in_tick, item.out_tick) for item in protected] == [(0, 100)]
    assert all(
        (item.source_id, item.source_sha256, item.clock_id, item.time_base)
        == (
            audio_context.source_id,
            audio_context.source_sha256,
            audio_context.clock_id,
            audio_context.time_base,
        )
        for item in protected
    )


def test_candidate_reusable_leaf_operations_reject_foreign_context_and_out_of_root_range() -> None:
    audio_context = _context(MediaKind.AUDIO, "root-audio-boundaries")
    transcript = _transcript(replace(audio_context, producer_id="sensevoice"))
    speech = _speech(replace(audio_context, producer_id="fsmn-vad"))
    foreign_context = replace(audio_context, clock_id="foreign:audio")

    with pytest.raises(DialogueGuardError, match="source audio clock"):
        group_transcript_words(transcript, foreign_context, word_gap_tick=7)
    with pytest.raises(DialogueGuardError, match="source audio clock"):
        merge_speech_activity(speech, foreign_context, vad_merge_gap_tick=2)
    with pytest.raises(DialogueGuardError, match="outside the source audio clock"):
        roll_protected_audio_ranges(
            ((audio_context.origin_tick, audio_context.end_tick + 1),),
            audio_context,
            pre_roll_tick=0,
            post_roll_tick=0,
        )
