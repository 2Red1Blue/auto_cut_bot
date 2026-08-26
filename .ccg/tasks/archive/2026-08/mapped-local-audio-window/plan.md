# Candidate video window → exact native audio extraction

This bounded pure slice implements §3 of task05's accepted local-window plan
and the audio-stream-facts followup. It is not a durable Command or admission.
It depends on the new exact audio facts and already accepted physical mapper.

Worker owns only new `physical_edit/local_audio_window.py` in Kernel and
`tests/media/test_mapped_local_audio_window.py`. No edits to source prep,
audio-stream-facts, existing mapper, old speech guards or other worker files.
Root owns metadata/review/commits. No Claude, recursive agents, native codecs,
models/DB, private configs, legacy inspection or worker commits.

Provide one public `derive_local_audio_window_spec(candidate_window,
presentation_map, audio_stream_facts, *, decoder_identity_sha256,
max_outward_padding_audio_ticks, max_source_bytes, max_decode_frames,
max_frame_bytes, max_pcm_bytes) -> LocalAudioWindowSpec`.
All parameters are explicit; no default limits, inferred sample rate, fake
mono, fresh Artifact schema or caller-owned persistence capability.

1. Require exact CandidateEvidenceWindow, ReplayedPresentationMap and
   AudioStreamFacts. Cross-check candidate source/hash/video clock/time base,
   complete source range and exact frame index against the map's root. Exact
   committed VLM ownership remains a later Command resolver responsibility.
2. Cross-check audio facts against the exact root audio index/context and
   presentation probe's selected audio stream/clock/range/execution hash.
   Reuse the facts module's public validation method when available; no copied
   decoder or imports from Pipeline.
3. Map both video boundaries, then choose the nearest actual audio boundary
   <= mapped floor(start) and >= mapped ceil(end), using ordered-index search.
   No synthetic endpoints, rounding inward, translating video ticks directly
   to audio ticks, source-origin rebasing, or stretching an A/V tail.
4. Enforce a nonnegative exact integer `max_outward_padding_audio_ticks` on
   each side against the exact rational mapped endpoint, not just floor/ceil.
   Thus zero padding does not silently allow fractional outward extension.
5. Require both complete A/V ranges inside a single proven continuous common
   interval through the unchanged mapper. Endpoints on either side of a gap
   do not establish complete coverage.
6. Construct LocalAudioWindowSpec from measured rate/channels/stream/clock and
   the selected audio range, exact source/audio-index hashes and explicit
   decoder/resource limits. Existing spec validation proves integral sample
   count and PCM bound; native extraction still verifies actual decoded frames.

Tests: different clocks/rates, multichannel, negative/fractional PTS, exact
end sentinels, nearest outward index membership, exact padding threshold,
internal gaps/unequal tails, missing/foreign facts/candidate/probe bindings,
invalid types/resources and deterministic replay. All pure synthetic values.
Returned spec alone does not authorize invocation or prove Source commitment;
the next child request must bind candidate, physical prelude and this spec.
