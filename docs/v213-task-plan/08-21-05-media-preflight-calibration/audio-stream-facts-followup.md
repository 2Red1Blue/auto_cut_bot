# Native audio layout required before local-window activation

Implementation checkpoint: optional typed audio facts, native producer
normalization and exact Source Command reconstruction are implemented and
independently reviewed. Pure/synthetic checks pass; real desktop native/DB
execution and local speech dispatch are not claimed. The physical prelude
keeps its frozen three-member shape.

## 1. Verified gap and version decision

`LocalAudioWindowSpec` requires positive `sample_rate` and `channels`.
`SourceMediaProbe` / `DecodedMediaProbe` currently persist audio stream index,
clock, time base and decoded range, but not those two layout facts. Existing
normalized ffprobe hashing also omits them. A native time-base denominator is
not proof of sample rate; channel count cannot default to one.

Use a closed optional `media_probe.audio_stream_facts` member with its own
`schema_version = audio-stream-facts-v1`. The existing top-level source manifest
has four keys and no schema-version field; its documented V2 designation refers
to mandatory presentation evidence, not an existing top-level version tag.
Do not invent a top-level v3 migration for this leaf extension.

Old bytes decode with audio facts explicitly absent and serialize with the key
omitted, preserving their canonical hash. Do not insert null/default facts.
New real source-prep output must include the measured leaf. Local-window
resolution requires it; absence is indeterminate before any invocation, never
permission to infer layout. Whole-source/read-only compatibility remains.

## 2. Required facts and ownership

Every field below is essential to local extraction or its provenance:

- schema version and original source id/hash;
- selected audio stream index and clock id;
- exact time base, decoded source origin/end;
- positive integer sample rate and channel count;
- exact audio-boundary-set hash;
- selected-audio normalized metadata (stream index, audio codec type, time base,
  declared start, sample rate, channels), with a recomputed canonical hash;
- probe executable/version/invocation identity already measured by source prep,
  bound to the same immutable source. Do not claim that the old normalized
  output hash covers new fields: the new leaf's preimage must include them.

Pipeline owns strict ffprobe normalization at the native boundary. In particular,
ffprobe's decimal-string sample rate is parsed as an explicitly documented
wire field, not accepted as a string inside the persisted integer grammar.
Missing/zero/float/bool/non-decimal/ambiguous audio layouts reject. Existing
source prep's one-selected-audio-stream rule stays in effect.

Kernel owns the immutable fact/closed decoder and compares its normalized
metadata against its typed fields, committed source/probe/index/clock and
decoded range. Probe metadata alone does not prove every decoded frame has
unchanged rate/channels: LocalAudioWindowTracker still verifies those on real
extraction and rejects drift, gaps, overlap and non-integral cuts.
Recomputing normalized metadata proves consistency, not that a tool executed;
execution provenance still belongs to the trusted producer and exact committed
Store. Raw-output replay would additionally require bounded raw ffprobe bytes,
not just a digest, and is not falsely claimed by normalized metadata replay.

## 3. Mapping into the actual request

After exact Source/VLM and physical-prelude readback:

1. Require the exact audio facts and matching certificate, root and candidate.
2. Map the video window's complete rational presentation interval; do not
   assign its ticks to the audio clock or compare only the two endpoints.
3. Select actual audio boundary ticks outward around floor(start)/ceil(end)
   under the frozen extension policy. No interpolation or inward shortcut.
4. Require both complete video/audio ranges to lie in a proven continuous
   common interval. A gap or single-stream tail remains indeterminate.
5. Build LocalAudioWindowSpec from the measured sample rate/channels and exact
   original audio range/index. Bind decoder and resource limits separately.

The presentation mapper accepts exactly the old eight-set root and the six-set
physical root after f0dc57b3, retaining certificate replay and all membership/
coverage rules. Certificate derivation supports both roots since f2c59f35.

## 4. Implementation and acceptance

One owner updates the shared audio-fact type/decoder, source-manifest optional
leaf roundtrip, SourceMediaProbe normalization/persistence and matching source
fixtures. Keep legacy decoder behavior separate; do not reread legacy code.
Update `source_prep/command.py`'s readback reconstruction as well as `probe.py`;
otherwise it drops the new leaf and breaks canonical equality on replay.
Next, the local-window owner consumes the exact leaf and maps the interval.

Tests must prove: old absent-leaf bytes/hash unchanged; new valid native facts
roundtrip; no rate/time-base equality assumption; multichannel retained;
source/stream/index/clock/metadata/hash substitution rejected even when outer
hashes are recomputed; missing layout prevents local dispatch; rational/negative
PTS, outward sample membership, unequal tails and internal gaps do not stretch.
Real source probe plus local extraction is verified on the desktop, not Mac.
No database table is needed merely for a nested immutable artifact payload;
actual generic Store persistence/ownership tests remain required.
