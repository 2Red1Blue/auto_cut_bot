# Lock real test authority profiles

## Goal

Enable an honest local-only run of the current drama with the user-approved
providers, without letting ordinary configuration or caller data become
authority. The exact VLM deployment is `doubao-seed-2-1-pro-260628`; timed
speech is native-host SenseVoiceSmall word timestamps plus an independent
FSMN-VAD producer. The resulting Pipeline may write only local evidence,
local Render/QC outputs and semantic highlight views; external publication
remains closed.

## Confirmed facts

- The pipeline runtime already uses the explicit Ark model identifier above;
  normal `auto_cut_bot.config.json` is not an authority input.
- A verified source/profile snapshot and matching PostgreSQL bootstrap anchor
  are required before the HTTP worker accepts media-preflight work.
- The current authority lock has no `registry_source` entries for timed speech
  or Stage 1 narrative policy bytes. An arbitrary self-consistent directory
  must not become a substitute.
- SenseVoice word timestamps and FSMN-VAD are physical-evidence producers.
  Their calibration record must be measured and hash-bound, not represented by
  a zero or guessed value.

## Requirements

- R1: Add protected authority source bytes for a shadow calibration profile
  and its successor run profile. Both identify the approved Doubao,
  SenseVoiceSmall and FSMN-VAD variants without credentials.
- R2: The shadow profile may run bounded golden/current-drama calibration but
  cannot enable HTTP media-preflight or publication. It records non-zero
  measured timing/error bounds and exact tool/model/policy identities.
- R3: Only a successful independently validated CalibrationRecord may be
  referenced by the run profile. It binds exact record, model identities,
  prompt/schema/parser hashes, word-gap/VAD merge policies and capabilities.
- R4: Publish sources through A -> B -> C: source commit, inventory commit,
  then generated lock from immutable Git blobs. Reject arbitrary/dirty source,
  mismatching repository/revision/path/blob and all-zero hashes.
- R5: Authority bootstrap uses the verified run profile, replays an identical
  profile/snapshot, terminally rejects divergence, and is unreachable from
  Pipeline HTTP.
- R6: Standard HTTP composition loads an injected verified snapshot and checks
  its durable anchor before accepting work. Missing bootstrap/profile/calibration
  fails before SourcePrep, Ark or FunASR work.
- R7: The first real local run uses the current drama, configured Ark
  deployment and native FunASR service; it makes no external publication call.
- R8: Stage 1 receives a separately locked Doubao narrative profile (model,
  prompt/schema/parser and coverage/dependency policies), never caller data or
  runtime environment profile.

## Acceptance criteria

- [ ] Protected closed sources identify `doubao-seed-2-1-pro-260628`,
  SenseVoiceSmall and FSMN-VAD; no secret occurs in source, lock, receipt,
  test or log.
- [ ] Shadow calibration creates a hash-bound non-zero CalibrationRecord and
  cannot start a Pipeline run.
- [ ] The run profile rejects substituted record/model/prompt/schema/parser/
  profile/source/RegistrySet identity.
- [ ] A clean A -> B -> C lock verifies Git blobs and rejects dirty/arbitrary
  bootstrap input.
- [ ] On `ac_autocut_verify`, migration -> verified bootstrap -> standard HTTP
  composition startup -> resolver replay succeeds; missing/divergent/zero
  profiles reach terminal denial.
- [ ] Real Pipeline cannot begin without verified injection, and no HTTP/API
  route bootstraps, rotates or selects a profile.
- [ ] A current-drama run stays local and never invokes publication.

## Out of scope

- Changing the selected model family, using Whisper/silencedetect, accepting
  arbitrary profile JSON, or publishing externally.
- Treating ordinary local configuration as authority.
