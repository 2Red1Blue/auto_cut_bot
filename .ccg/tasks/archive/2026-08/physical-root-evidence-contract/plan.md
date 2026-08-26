# Physical-root contract checkpoint

This is the first implementation batch of task05's physical/local split, not
the complete local Runtime. Claude owns only new `media/physical_root.py`,
`media/physical_root_codec.py` and `tests/media/test_physical_root_evidence.py`.
Existing root evidence, its codecs, service and all consumers stay read-only.

`PhysicalRootMediaEvidence` is an immutable canonical value containing
`physical_root_id`, source id/hash, source-manifest hash, root-input-manifest
hash, and exactly six existing typed sets: frame PTS, shot, scene, audio sample,
visual validity and subtitle cues. It contains no transcript, VAD, approval,
policy defaults or fabricated silence. Source/probe/certificate ownership is
resolved by the later Command; this value alone never grants that ownership.

Validate exact set types and complete source-bound coverage; all five video
sets share exact clock/time-base/origin/duration. Audio retains its own native
clock and may have unequal source tails. Shot/scene bind the exact frame hash
and every boundary is an actual frame member. Preserve all six set hashes;
the new aggregate has its own hash. Keep the v1 eight-set bundle unchanged.

Strict mapping and bounded JSON decoders reuse the public six-set decoders and
`decode_media_evidence_json`, rejecting unknown/missing keys and malformed
types without coercion. Do not call private helpers or construct a dummy
eight-set root. Test source/clock/coverage/frame-hash/member mutation, missing
or injected speech fields, JSON duplicates/numbers/bounds and preservation of
old root behavior. Pure tests only, no model/native codec/DB execution on Mac.

Root reviews Claude's accompanying durable-window plan separately: source/video
and audio ranges require a proven presentation map, ambiguous success commits
must reconcile rather than reject, unknown invocation cannot be bypassed with
a fresh key, and local calibration needs a real pre-acceptance measurement
path. None of those unfinished seams are implemented or accepted by this DTO.
