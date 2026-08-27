# Reuse-aware calibration and timed-evidence activation

## Decision

Historical facts are immutable. A calibration record, media-evidence child,
Receipt, ArtifactSet, or rendered result is never relabelled `stale` and never
overwritten. Whether it is usable is a decision about a *new target*.

```text
target requirement + exact historical closure
    -> reusable | recompute_needed | awaiting_calibration
```

`blocked` remains a publication decision only: it means the selected target
still lacks a closed required Evidence -> Recipe -> Render -> QC chain.

## Two independent reuse questions

```text
Can this exact historical evidence be reused?
    source/proxy/semantic/physical/policy requirement fingerprint matches

Can this machine create new timed-speech evidence?
    accepted capability for its live timing-compatibility identity exists
```

The first answer must not depend on the current machine. A complete PC evidence
closure imported into Mac is still auditable and reusable. The second answer is
environment-specific: a PC CUDA record does not authorize Mac to generate fresh
ASR/VAD evidence.

## Runtime calibration capability

A static protected CalibrationPolicy declares allowed runtime capability IDs
and device families. Each accepted record persists one closed identity:

```text
RuntimeMeasurementIdentity
  = runtime_capability_id
  + complete TimingCompatibilityProfile
  + audit-only build identity

RuntimeCalibrationCapability
  = static policy/profile + registry snapshot
  + RuntimeMeasurementIdentity
  + exact accepted record/validation closure
```

The canonical identity includes `timing_compatibility_sha256`; ordinary audit
code changes are retained for provenance but do not invalidate the capability.
PC CUDA and Mac CPU use distinct immutable capability scopes/anchors. Legacy
v1 records stay readable as history but cannot authorize a v2 runtime.

The FunASR service receives no PostgreSQL credential and does not decide
whether a record is accepted. It authenticates the Pipeline and compares the
request's expected compatibility hash with its self-measured live identity. The
Pipeline independently reads the accepted capability immediately before the
request and writes that exact capability reference into the resulting
Receipt/evidence closure.

## CUDA Command and Receipt boundary

The CUDA command accepts the authenticated `RuntimeMeasurementIdentity`, not a
caller-built projection. Before it claims native work it re-reads the matching
accepted capability from the Store and derives one
`RuntimeTimedSpeechProjection`; that projection is included in the actual
Command hash and in the `runtime_timed_speech_capability_admission` Artifact.

The CUDA producer receives that exact projection and must call only the
dedicated v2 CUDA endpoint. Its v2 provenance carries the corresponding runtime
policy mapping. The installed CUDA authority resolver also carries the hash of
the protected static operation policy used to construct that mapping; Kernel
admission requires exact equality, rather than accepting a caller-provided
policy hash. It independently verifies the closed policy schema and v2
loopback route, projection hash, runtime versions, static profile/registry,
record/validation hashes, source clock, timing-policy hashes and gaps, ASR/VAD
producer identities, timing bounds and native-port adapter hash. CPU v1 and
CUDA v2 provenance are command-specific grammars: neither command accepts the
other's evidence. A successful command uses the shared Store
`artifact_set_hash`; a success-commit acknowledgement error is indeterminate
and must be reconciled, never overwritten with a rejection.

The CPU reader/finalizer is deliberately not a compatibility adapter. The
runtime five-member layout has its own exact reader and batch finalizer; the PC
Media Preflight stage uses that distinct completion chain. The editorial-input
join dispatches to the matching reader by exact batch grammar, so a CUDA
ArtifactSet can never be interpreted as a CPU `local_run` result. The CUDA
policy mapping and service route are both v2
`/v2/runtime-timed-speech-evidence`; V1 is CPU-only.

## Pipeline waiting and recompute states

Pipeline startup validates static configuration and service health, not the
existence of a calibration capability. Timed-speech work is classified before
any source materialization, detector call, or FunASR request:

```text
matching accepted capability                    -> pending/running
no matching accepted capability                 -> awaiting_calibration
changed requirement or live compatibility drift -> recompute_needed
```

Both states are durable, receipt-less control-plane states. Workers do not
auto-lease or reconcile them. A new capability may explicitly wake an
`awaiting_calibration` target; an old run with a changed requirement never
silently resumes and instead requires a new idempotent run/generation.

Temporary infrastructure outcomes (timeout, `BUSY`, GPU OOM) remain bounded
retries of the same requirement. A model, decoder, policy, source, proxy map or
semantic-input change is a new requirement and never a retry.

## Requirement fingerprint and evidence index

`EvidenceRequirement` is a canonical value object whose
`requirement_fingerprint_sha256` contains only inputs that change the evidence:

- episode/window/source content and source/proxy time-line identities;
- semantic-pack identity where candidate-derived evidence depends on VLM;
- physical detector/index/boundary and adaptive-plan policies;
- timed-speech profile/capability, authority snapshot, strategy and schema
  versions.

It deliberately excludes Job keys, command/receipt/set UUIDs, Blob object IDs,
idempotency keys, staging limits and other operational ownership values.

`EvidenceIndexEntry` maps one target episode and fingerprint to exact succeeded
child handles: source Job/profile, command slot, Receipt, ArtifactSet, request
and set hashes, plus ordered immutable member references. It is valid only if
the Store re-reads and verifies that exact closure. It never uses a logical head
or a broad "latest" query.

## Cross-Job composition

The existing same-Job `FinalizeTimedMediaEvidenceBatch@2.1.3` contract is
unchanged. A new append-only `ComposeWholeEpisodeEvidence@2.1.3` command
creates a fresh aggregate for a target episode census. It may select:

```text
episode 1 -> exact succeeded child from PC Job A
episode 2 -> exact succeeded child from Mac Job B
...
episode N -> exact succeeded child from current recompute Job C
```

The command rejects missing, duplicate, reordered, corrupt, foreign-profile or
requirement-mismatched entries. It commits only when every target episode has
exactly one verified match; old rows are read-only. A legacy same-Job batch can
contribute only through a strict adapter that receives its complete exact handle
and derives the requirement from its persisted members.

## Minimal persistence boundary

Persist only what is authoritative or needs efficient exact lookup:

```text
CalibrationRecord v2 / immutable anchor       # environment-specific capability
EvidenceIndex                                 # exact reusable child lookup
PipelineRun states                            # awaiting/recompute operational state
WholeEpisodeEvidence aggregate                # atomic selected target closure
```

`ReuseAssessment` is a pure, cacheable computation; do not add a mutable stale
column. Do not introduce a generic graph database in this slice. Persist direct
input references in each new composition payload; expand to a general
derivation-edge table only when later Stage 4/Render work needs recursive impact
planning.
