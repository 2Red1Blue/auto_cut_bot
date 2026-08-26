# Exact committed timed-media reader wave

## Scope and prerequisites

Continue Task06 from the delivered codecs. A decoded value is not a committed
predecessor. Preserve the VLM-first semantic owner and separate frame/audio
clocks; no legacy, fixture Recipe, publication or local real execution.

Read-only checks found two producer gaps to fix at their owner before reading:
PrepareTimedMediaEvidenceRequest lacks an exact committed VLM selector, and an
empty candidate index bypasses the candidate-owned full calibration-set check.
The root member must persist all eight complete calibration bindings, including
time base, active flag and explicit nullable adapter. Missing historical fields
are rejected; no inferred defaults or compatibility backfill.

## Implementation and ownership

1. Contract agent: Prepare command/request and existing timed-evidence binding
   validator plus their tests. Require CommittedSemanticInputsRequest, reread
   exact Source/VLM before effects and bind both full references into command
   identity. Extract reusable resolve/hash/produced-validation/candidate-replay
   functions from the existing producer implementation. Apply binding checks
   for zero candidates too, without fabricating candidates.
2. Runtime agent: media_preflight_stage.py and its focused tests. Pass the actual
   committed semantic selector already read by the Runtime. No Runtime-private
   evidence authority, synthetic selector or fallback.
3. Root: new pipeline/committed_timed_media.py and focused tests. Validate the
   succeeded outcome and exact five-member Store record, scope/revision/ordinal/
   logical identity/request hash. Decode bounded blobs with the existing codecs,
   rerun Source/VLM/root/binding/plan closure, profile admission and the v2 clock
   certificate. Persisted hash/pass declarations cannot replace recomputation.
4. Independent reviewer: read-only boundary and tampering review; source owner
   fixes findings. Tests use producer-shaped in-memory committed records, not a
   claim that real PostgreSQL or models were exercised.

No overlapping writers. Use the existing active CCG task and global Trellis
mirror. Commit coherent tested slices promptly on feat/v213-contract-codegen.

## Reader bounds and authority

Validate BlobRef metadata, expected MIME, per-blob and aggregate byte ceilings
before materialization. Read only the leased private file with a bounded read;
verify exact lease reference, byte length and raw hash, and close on every path.
A Blob raw hash and media canonical value hash are different identities.

Recompute all candidate plans in the actual committed VLM order; zero candidates
is valid only for the actually empty VLM pack and still validates root bindings.
The sibling probe must equal the actual SourceManifest probe. Recompute speech
admission and map segments/non-overlap from the accepted profile, evidence and
calibration binding; never flatten the v2 map to a duration ratio.

Registry membership alone does not prove accepted ASR/VAD calibration bounds.
The production consumer must reuse the installed local-run resolver and exact
accepted CalibrationRecord binding, and compare persisted producer/clock/bound
values to that installation. Other detector bindings do not inherit an ASR/VAD
acceptance claim.

## Following dependent slice

The current batch child names only episode/key/Receipt/Set and the finalizer
checks outcome only. Extend its identity with exact slot/request binding,
reconstruct actual Source episode coverage and validate every five-member child.
Then the Stage3/Catalog join consumes this exact batch, followed by committed
piecewise-clock compilation, physical Admission, production A/V Recipe and
Render/local QC. This child-reader wave is not full Task06 completion.

## Following batch slice: memory and module ownership

Move batch DTOs/Command into a dedicated
pipeline/finalize_timed_media_evidence_batch_command.py owner, with its exact
batch reader. Dependencies stay batch -> committed_timed_media -> prepare;
update real facade imports and leave no compatibility alias in prepare.
Children carry actual typed Prepare requests and exact succeeded outcomes.
Persist slot/request/Receipt/Set and all five references, and compare episode
coverage to the actual SourceManifest rather than the supplied child count.

Do not derive JSON limits from Source upload or staging limits: the non-secret
FunASR deployment template permits 2 GiB requests. Multiplying that allowance
by candidate count is not a safe evidence-memory policy. The next Runtime wire
must explicitly add evidence_read_limits with max_blob_bytes and
max_total_blob_bytes; candidate count comes from the frozen VLM parse policy.
This requires execution-profile v9 and a closed migration, not defaults for
old v8 rows. Current execution remains v8 until that implementation lands.

The batch total is cumulative across every episode, not reset for each child.
Validate all lightweight committed metadata and sum Blob lengths before any
materialization; then validate one child at a time and retain only compact exact
references/hashes. Release heavy root/plans/candidates rather than retaining
45 full episode DTOs. Stage4 later rereads the selected episode by exact identity.
No episode is omitted from batch validation, including zero-candidate episodes.
Serialized-byte ceilings do not promise an equal RSS ceiling: decoding and replay
may temporarily hold several representations. Add multi-episode cumulative-limit,
zero-blob-read-on-overflow and sequential-release regression tests.

## Acceptance checks

- Forged/missing/cross-job Source or VLM rejected before producer work.
- Empty candidates cannot bypass an invalid producer clock/binding.
- Missing/reordered/foreign five-member sets and failed/stale outcomes rejected.
- Rehashed altered root, plans, candidate owners, profile or certificate rejected.
- Bounds checked before reads, corrupt bytes/leases rejected and leases closed.
- Valid empty/nonempty producer output survives exact roundtrip without raw-hash/
  canonical-hash confusion; no detector rerun during reads.
- Pure tests, scoped Ruff/types and independent review. Real migration/provider/
  full Pipeline acceptance remains on the desktop.
