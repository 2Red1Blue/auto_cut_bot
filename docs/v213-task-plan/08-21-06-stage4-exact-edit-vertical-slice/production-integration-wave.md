# Stage4 committed-media integration wave

## Current boundary

Active reader slice: [Exact committed timed-media reader](committed-reader-wave.md).

Stage1-3 shared Commands and their exact readers now exist. The earlier Task06
editorial-fixture allowance is obsolete for new production work: the production
input is the actual admitted Stage3 batch and its exact Stage2/Catalog/root
predecessors. This wave does not activate external publication.

Read-only inspection found usable exact A/V primitives, but no durable production
Stage4 Command. Existing LocalMediaCommand/fixture Recipe/video-only Render remain
test infrastructure, not an alternate production path. Do not merely wrap them
in an HTTP adapter.

## Implementation order

1. Strict persisted evidence codecs: root evidence, candidate timed evidence,
   window plans, presentation probe/certificate and timed-profile admission.
   Decode the existing producer wire shape without changing it. Constructors own
   domain invariants; codecs own closed fields, actual JSON types, canonical
   value preservation and nested decoding. Unknown/missing/null/float/bool-as-int,
   invalid enum, changed source/clock/coverage and noncanonical rationals reject.
   Optional absence is allowed only where explicitly emitted by the producer.
   Decoded DTOs are values, never proof that their input was committed or safe.
2. Exact timed-media reader: use read_committed_artifact_set and bounded Blob
   materialization. Validate the batch's exact child Receipt/Set membership and
   every fixed five-member child; replay Source/VLM/probe/profile/calibration
   bindings. Do not accept a caller-built batch of unrelated successful children,
   a legacy media_evidence member or a producer's boolean as admission authority.
3. Stage3/Catalog physical-input join: retain exact Stage3 request/outcome and
   re-read its admitted 3N+1; join alternatives to the real Catalog member,
   then raw VLM candidate owner/hash and matching candidate evidence window.
   Preserve frame-index/source/clock/time-base/window/request identity, current
   render_source authorization, all source constraints and physical requirements.
   ASR text never enters Stage1-3. Stage3 material assignment is a semantic
   witness, not the final physical choice.
4. Exact compiler consumes committed v2 presentation-map segments without
   flattening gaps/non-overlap or inventing probe/calibration claims. Editing
   mode (dialogue/action) and SpanPolicy (tight/scene/context) remain distinct.
   Word/VAD protection cannot satisfy a complete-dialogue requirement.
5. Independent physical evaluation, production A/V Recipe and atomic Stage4
   Command/replay, then Render/local QC and Runtime registration. Empty/partial
   Recipes never become success. Rendering must retain required audio, not reuse
   fixture_ground_truth_v1 or a video-only -an plan.

## First parallel slice: codecs

- Root-evidence owner: new media/root_evidence_codec.py and
  tests/media/test_root_evidence_codec.py only. Export strict decoders for the
  root bundle and shared context/coverage/frame/sample/Transcript/VAD values
  needed by the candidate codec. Do not alter root_evidence domain classes.
- Candidate owner (after the public decoder signatures freeze): new
  media/timed_evidence_codec.py and tests/media/test_timed_evidence_codec.py.
  Decode the existing CandidateEvidenceWindow/Plan/TimedEvidenceSet wire shapes;
  reuse root codecs, no copied nested wire parsers.
  Review exposed an existing CandidateEvidenceWindowPlan constructor indexing
  windows before checking assessment cardinality. This owner may also move the
  existing outcome/count validation before that access in media/timed_evidence.py;
  fix the domain owner rather than catching IndexError in the codec. No change
  to valid plans or their wire format is authorized by this correction.
- Root presentation owner: media/presentation_evidence_codec.py and
  tests/media/test_presentation_evidence_codec.py for existing v2 certificate/
  profile-admission value bodies. Reuse root decode_time_base. The actual
  SourceManifest reader already decodes PresentationTimelineProbe; reuse that
  verified value and compare the persisted sibling, not a second probe decoder.
- Root also owns task/design progress, exact-reader planning and commits.
- Independent reviewer: read-only domain/codec roundtrip and malicious-shape
  tests; must not approve a decoded DTO as committed authority.

One writer per file. No legacy imports, compatibility aliases, default filling,
second evidence model, new governance framework or package facade churn.

## Verification and remaining acceptance

Use real producer-shaped synthetic values for unit roundtrip and focused
mutation tests, including no-audio, no-lexical/VAD, missing coverage, negative
source origins and rational clocks. Preserve bytes/hash on canonical roundtrip;
never use Python dict equality alone to accept integer/float substitutions.
The reader's later tests must cover rehashed forged owners and missing or
cross-job committed members, not only bad syntax.

Run scoped pytest, Ruff, types, independent review and save coherent commits
on feat/v213-contract-codegen. No local DB/migration/model/service/full Pipeline.
Desktop owns real calibration, PostgreSQL restart/replay and episode acceptance.
Codecs alone do not complete Task06, grant a Recipe or enable whole-run success.

## Delivered codec checkpoint

4955d1a7 implements root, candidate timed-evidence and presentation-certificate/
profile-admission codecs using the existing wire/domain types. Final local
codec/domain/preflight/architecture suite: 1795 passed; Ruff and production
types clean. Independent read-only root/timed/presentation reviews accepted
the slice, including the constructor count-check ordering correction above.

Cross-codec tests decode actual in-memory preflight output, read the probe via
the existing SourceManifest decoder and replay both clock-certificate and
profile-admission inputs. A fully rehashed changed snap allowance is still
rejected by probe replay. These tests do not establish real detector execution,
SQL commitment or a production Stage4 admission.

Next: the exact five-member/whole-batch reader with bounded materialization and
committed Source/VLM/Registry/calibration joins. Codecs must be consumed there;
do not reopen codec design or treat arbitrary decoded values as that reader.
Then implement the Stage3/Catalog physical join and production A/V editing.
