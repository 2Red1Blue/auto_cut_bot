# Design — Locked Shadow Calibration and Local Run Profiles

```text
protected Git source -> shadow calibration profile -> CalibrationRecord
  -> protected run profile -> verified local-profile identity snapshot
  -> authority-only PostgreSQL bootstrap anchor -> HTTP runtime injection
  -> local-only Pipeline work
```

Pipeline HTTP and ordinary configuration never choose a profile. Credentials
remain deployment secrets; authority source contains only identities and hashes.

## Profile states

`shadow_calibration_v1` freezes the selected model/detector identities,
calibration corpus/window policy, source-clock rules, word-gap/VAD merge policy
and non-zero acceptance criteria. It may invoke calibration but cannot enable
HTTP media-preflight.

`local_run_v1` is a successor source profile. It repeats the frozen policy and
adds the exact CalibrationRecord member hash. Only this state may supply the
local-profile identity snapshot, be bootstrapped into the authority Store and
be injected into runtime.
New calibration means a new profile version, never an anchor mutation.

### Compatibility identity versus build audit

The complete service build SHA remains recorded for audit and incident
reproduction, but it is not alone a calibration invalidator.  A calibration
record binds a closed timing-compatibility identity derived only from the
timing-relevant engine version, ASR/VAD model trees and versions, device class
and measured capability, decode/resample implementation, native protocol,
word-timestamp/VAD policies, and ordered producer identities.  The build SHA
is retained beside it so a reviewer can see the exact bytes that ran.

Changing a log line, health route, UI/API code, or VLM/story policy may create
a new audit SHA while preserving timing compatibility; an accepted compatible
record remains usable.  Changing model bytes, CPU/GPU class or CUDA runtime,
decoder/timestamp/VAD behavior, timing-engine compatibility version, or a
producer identity changes the derived compatibility identity and therefore
requires fresh shadow measurement and independent validation.  The runtime
never accepts a caller-supplied compatibility hash: the source compiler and
the native service each recompute it.

A current build with no accepted compatible record is allowed to start only as
**shadow-only**.  Its calibration endpoints are available, while ordinary
`/v1/timed-speech-evidence` remains denied.  Only a separately installed
local-run profile with a durable accepted compatible record can enable normal
Pipeline detector use.  This keeps fail-closed publication safety without
making unrelated code changes turn into a total startup outage.

## Calibration authority boundary

The existing `calibration_record_sha256` consumer field is not a
CalibrationRecord. Before a run profile can exist, the Kernel must own a
closed immutable CalibrationRecord member and independent validation receipt.
The record binds the shadow-profile source/RegistrySet identity, immutable
corpus members and anchors, source clocks, the exact native ASR and VAD model
trees/service bytes/versions, every measurement artifact, the deterministic
bound calculation and strictly-positive accepted ASR/VAD bounds. It is not
valid when produced from caller JSON, a model directory name, a fixture,
self-reported service profile, OCR, zero values, or guessed values.

The shadow calibration command is local-only and cannot construct Pipeline
HTTP or publication services. It first writes measurement evidence, then a
candidate record, then an independent validator either commits a validation
receipt and immutable record or commits a terminal denial/indeterminate
receipt. `local_run_v1` may name only a committed record and validation receipt
whose profile, model, policy, source and RegistrySet identities exactly match.
This makes a measurement-dependent profile a later protected source revision,
not a mutable bootstrap-anchor update.

The measurement evidence is not an opaque response blob.  For each corpus
member it must contain a closed, calibration-only
`shadow-calibration-funasr-raw-response-v1` envelope and a separately stored,
canonical full-source invocation mapping.  The envelope preserves the direct
SenseVoice word timestamps and FSMN intervals, binds the request/source clock,
range, policies and measured shadow-native identity, and deliberately contains
neither `calibration_record_sha256` nor a timing-error bound.  The normal
`timed-speech-evidence-response-v1` cannot bootstrap the first record because
its ordinary service identity already assumes those fields.  The independent
validator re-decodes the raw envelope with duplicate-key rejection, recomputes
integer observations, VAD merge, anchor pairing and positive maxima, and then
compares that result to the measurement projection.  A blob hash or the
measurement command's projection alone is never evidence of calibration.

The validator uses a dedicated authority Job derived from the canonical shadow
profile key, not the one-shot timed-speech bootstrap Job. A successful command
terminals its Job; therefore each new profile version has its own calibration
validation Job, protected calibration scope, logical record ID and immutable
anchor. Generic Store writes remain unable to create that protected record or
anchor.

### Frozen Phase-2 persistence contract

The accepted validation ArtifactSet has exactly four members in this order:

| ordinal | artifact type | role |
| ---: | --- | --- |
| 0 | `calibration_record` | aggregate record |
| 1 | `calibration_record_member` | SenseVoice ASR child record |
| 2 | `calibration_record_member` | FSMN-VAD child record |
| 3 | `calibration_validation_receipt` | independent accepted decision |

All members use scope
`autocut_authority/calibration/shadow_calibration@<profile_version>`, revision
`1`, and fixed logical IDs beneath
`calibration-record/{aggregate|member/asr|member/vad|validation}/<profile-key>/1`.
The aggregate content hash is distinct from both child hashes; the two child
hashes are distinct from each other and are the values projected into the
existing ASR/VAD Registry requirements.  The validation member and generic
Command Receipt are different objects: the member is stable source-bindable
evidence, while the generic Receipt proves the Store transaction and command
outcome.

`ValidateCalibrationRecord@2.1.3` runs under authority Job
`autocut_calibration_validator:<profile-key>`.  Its request contains only exact
committed references to the prior measurement manifest/results and the locked
shadow profile identity. It re-reads every immutable raw blob, rejects duplicate
keys, recomputes alignment and integer bounds, and compares the stored
projection without trusting it. Accepted writes all four members, the succeeded
Command Receipt and one immutable `calibration_record_anchors` row in a single
transaction. Deterministic invalid evidence writes only a denied Receipt;
unavailable evidence writes only a failed/indeterminate Receipt. Neither branch
creates an ArtifactSet or anchor.

The validator is read-only with respect to providers, so process loss before a
commit is safe to replay through the existing command idempotency boundary. It
must not reuse or extend the native-invocation recovery state from migration
0016. The anchor has no mutable current pointer and cannot be updated or
deleted.

Successful calibration authority Jobs use a narrowly scoped finalization
variant: their exact validator Receipt/four-member set/anchor closes the Job
with no open slots; ordinary Pipeline Jobs retain the existing
`FinalizeRunOutcome` requirement. Validator Job key/profile cannot mutate in
any state. A terminal failed Receipt is replay-only under its key; an explicit
bounded retry uses a new attempt key over the same immutable inputs. Both
Receipt-to-set and set-to-Receipt/anchor constraints must hold, including when
a later transaction tries to attach artifacts to a denied/failed slot.
Shared aggregate/child identity fields are nested under the closed `identity`
JSON object, and SQL closure must use that exact shape.

### Frozen independently-validatable measurement input

The current `shadow-calibration-measurement-manifest-v2` is not a sufficient
validator input: it persists the invocation and raw BlobRef but drops the
canonical `raw_context` containing the expected ASR/VAD anchors.  A validator
over v2 could only trust the measurement projection and therefore cannot
produce an independent receipt.

Before `ValidateCalibrationRecord@2.1.3` is implemented, measurement
finalization must emit `shadow-calibration-measurement-manifest-v3`.  Each
ordered manifest member contains exactly the corpus-member reference, expected
anchor-reference hash, native invocation, complete canonical `raw_context` and
immutable raw-response BlobRef.  The results member remains the untrusted
projection used only for equality comparison.  The validator rejects v2 as
non-validatable, rereads the exact two-member succeeded measurement set and raw
Blob bytes, reconstructs every typed anchor/context, re-decodes the native
response and recomputes the integer bounds.  No logical-head lookup or caller
supplied context is permitted.

The record vocabulary follows this predecessor boundary:
`registry_snapshot_sha256` names the measured RegistrySet snapshot;
`producer_kind` is `asr|vad`; model IDs are exactly `SenseVoiceSmall` and
`fsmn-vad`.  The bound-algorithm identity is a Kernel-owned deterministic hash
of the frozen aggregation/alignment algorithm, not a caller-selected field.
ASR and VAD producer IDs, detector hashes, model IDs, model hashes and child
record hashes must all differ.

The pure media facade may decode and verify candidate/committed record bytes,
but it must not offer a public function that manufactures an accepted
validation receipt from caller-provided hashes.  Accepted assembly is an
internal validator-command seam and consumes only independently recomputed
proof material.  This API boundary prevents accidental self-certification;
the protected Store command and PostgreSQL transaction remain the actual
authority boundary.

## Shadow measurement recovery

Native calibration is not treated as a pure retryable function after a process
or Store-commit interruption.  Before a native call, the command persists the
closed member plan (invocation, locked context and anchors) in a shadow-only
attempt aggregate.  A leased member transitions `pending -> invoking ->
staged`; staging the raw response, BlobRef and decoder-derived projection is
one Store transaction.  The final two-member ArtifactSet/Receipt is also one
transaction over staged members only.

If a worker loses its response after a committed stage, an expiring recovery
lease discovers and finalizes the durable stage without invoking FunASR again.
If a worker may have begun native inference but no stage exists after its lease
expires, that member is `indeterminate`, not failed or silently retried.  A
successor attempt preserves the predecessor and requires a bounded, recorded
retry authorization.  Known pre-invocation resource/connection failures may
retry within that same bounded policy; unknown native outcomes never do.  This
specialized aggregate changes neither generic `command_slots` replay nor VLM
recovery semantics.

| role | identity |
| --- | --- |
| semantic VLM | `doubao-seed-2-1-pro-260628` via Ark streaming |
| word ASR | native CPU SenseVoiceSmall, `output_timestamp=true` |
| speech activity | distinct native CPU FSMN-VAD |

## Source and lock

Place closed profile/RegistrySet sources beneath protected `governance/`, and
list every file as `registry_source` in the authority inventory. The existing
A -> B -> C protocol applies: reviewed source commit A, sole-child inventory
commit B, then generated-lock commit C computed solely from B and A Git blobs.
An authority build/admin command verifies those Git blobs, compiles the closed
source set, and emits one immutable packaged authority-context resource. The
runtime never reads a checkout, `tools/`, Git path, commit, profile selector or
ordinary configuration: it reads only that installed resource and then checks
the durable PostgreSQL anchor.

## Runtime and rollback

Server deployment receives an immutable verified snapshot from the packaged
authority-context resource. It resolves the exact local-run profile and durable
anchor before worker reconstruction or outbox leasing. Any absent/zero/unknown
resource, missing anchor or record mismatch denies startup. HTTP has no
bootstrap/profile selector.

Identical bootstrap replays its Receipt; divergent identity terminates as a
conflict/rejection Receipt with no running slot. Stop a bad deployment and
deploy the previous verified profile; retain immutable anchors/records and
never delete or rewrite them.

## Delivery sequence

```text
closed raw-envelope/recoverable measurement/CalibrationRecord contract + shadow source
  -> protected A/B/C lock
  -> bounded native CPU measurement + independent record validation
  -> protected local_run_v1 source naming that record
  -> second protected A/B/C lock
  -> verified admin bootstrap -> injected HTTP snapshot -> local-only run
```

The authority-source A commit also requires an explicit tracked authorization
for this authority child; the user approval that started this task is recorded
as that approval's provenance, but a generic task must never self-authorize at
runtime. The B and C commits remain inventory-only and generated-lock-only.

## Build integration slice: locked Registry and shadow context

Freeze implementation ownership to `tools/authority/locked_registry.py`,
`tools/authority/shadow_context.py`, `tests/authority/test_locked_registry.py`,
and `tests/authority/test_shadow_context.py`, plus this task's planning record.
This is inside the existing authority-child grant. Do not modify real source
profiles, inventory/lock, runtime, ordinary CLI or package installation in
this slice.

The build entry reads an explicit C Git blob (not a checkout lock), derives A
and B from it, replays the existing A/B/C verifier and verifies every locked
blob. The selected Registry tree must consist only of lock-covered
`registry_source` files. Copy those exact regular Git blobs to a private
temporary directory, preserve their fixed eight-pack relative paths, call the
existing source loader/compiler, require ready, and reject missing or unused
files. Do not accept a caller-created RegistrySet or source mapping.

Shadow context construction reuses that verified compilation and resolves
narrative/shadow raw profile bytes only from exact `registry_source` entries.
The expected shadow contract hash comes from the locked raw
`governance/schemas/shadow-calibration-profile.schema.json` `schema_source`
entry, matching the existing profile grammar tests; it is never a repeated
caller-provided hash. Reuse the existing grammar decoder. Native service-identity
recomputation remains owned by the existing service projector at calibration
input resolution, before any I/O; do not duplicate it or import the app into
tools/Kernel. Report source/lock/registry provenance separately from calibration
acceptance. This shadow context may feed calibration only, never bootstrap or
HTTP composition.

Current published lock has neither target schema nor registry/profile entries.
Positive tests must use explicitly synthetic A/B/C repositories; no successful
fixture becomes a deployed authority profile. Dirty checkout bytes are never
read or accepted; an explicit immutable historical commit remains usable despite
unrelated checkout/config changes.

Local-run requires two distinct provenance chains: its predecessor reference
must equal a separately verified shadow source, shadow Registry snapshot and
shadow lock bundle. The existing grammar only validates the latter two hashes'
syntax, so current C cannot certify old C. Installed-resource publication and
local-run/accepted-anchor loading are subsequent work, not implied by this
shadow-only build slice.

## Local-run predecessor and accepted-anchor integration

The next implementation owns only tools/authority/local_run_context.py,
tools/authority/local_run_calibration.py and their matching authority tests,
plus this task record. No runtime permit, bootstrap call, installed resource,
real source publication, native call or Store write is part of these helpers.

The local-run source builder takes explicit current Git selectors and a frozen
ShadowSourceSelection (lock repository/commit/path, Registry repository/root,
profile repository, narrative path, shadow path). It internally calls the
existing verified builders for both chains; a caller-created context or Registry
is not an input. Decode the current locked narrative and local-run raw source
using the current locked local-run-profile.schema.json. Compare predecessor
version, raw source, Registry source_hash and lock bundle_hash against the old
independently verified shadow context. Current C cannot stand in for old C.
The local-run grammar closes native/narrative/clock/timing inheritance. Return a
source context only; calibration references remain unresolved at this boundary.

A separate read-only binder consumes that builder's context and the existing
Store read_calibration_record_anchor seam. Read exact local-run record/validation
refs using the predecessor shadow source and predecessor Registry hashes, not
the current Registry. Compare the returned aggregate identity in full, both
producer common identities, child hashes/positive bounds and ordered corpus
member/anchor references against locked sources. Reuse the existing record-set
decoder/verifier and reader closure; do not reimplement raw inference validation.
Return the existing PersistedCalibrationRecordAnchor, never a runtime snapshot.

Source contexts and typed anchors are not capabilities: production composition
must call the verified builder and real Store reader. Unit fakes must be explicitly
named and cannot establish deployed authority. Store integration keeps its real
0/3 aggregate/validation ordinals and canonical logical IDs; simplified grammar
fixtures are not accepted-record fixtures.

The timed-speech entry registry_contract_sha256 currently has syntax/full-entry
hash binding only; no canonical contract derivation exists in code. Define and
test that source binding before installed-resource/bootstrap activation rather
than substituting the profile schema or Registry hash. This is not satisfied by
the source-only helpers in this slice.

## Timed-speech Registry contract identity

The previously opaque registry_contract_sha256 receives its first explicit
derivation; no deployed v2.1.3 profile is migrated and no duplicate schema is
introduced. The input is the already Git-verified raw local-run profile schema,
not caller JSON, an invented digest or the whole profile/Registry hash.

The Kernel-owned timed_speech_registry_contract_sha256(raw) strictly parses the
source with the existing canonical JSON loader, requires Draft 2020-12 and the
top-level properties.timed_speech_registry_entry value to be exactly
{"$ref":"#/$defs/timed_speech_registry_entry"}. Its canonical hash material is:

- schema_version = timed-speech-registry-contract-projection-v1
- schema_dialect = https://json-schema.org/draft/2020-12/schema
- root_pointer = #/$defs/timed_speech_registry_entry
- definitions = the root definition and every transitively referenced $defs entry

Only exact #/$defs/<name> references are supported (plain nonempty identifier
names, no pointer escapes/subpaths). Traverse deterministically with a visited
set. Reject missing/external/unsupported refs, nested $id/$schema and dynamic or
recursive reference/anchor mechanisms that could alter resolution. Each selected
definition must be a schema object or boolean. Unrelated profile definitions,
formatting and mapping key order do not affect the projection; any reachable
schema content does. This identifies the wire-schema closure, not the complete
semantic decoder/implementation and not a calibration or runtime permission.

The locked local-run builder derives the digest from schema_raw and compares it
with the decoded Registry entry before returning. Grammar-only APIs remain
explicitly unresolved. Existing semantic inheritance and accepted-anchor checks
are unchanged. Tests must check an independently assembled projection, cycles,
scope/ref rejection, reachable/unrelated changes and a Git-locked profile that
substitutes a wrong contract hash.

Ownership for this slice: Kernel registry/timed_speech_contract.py and its new
authority tests (worker); tools/authority/local_run_context.py, the two existing
authority profile/source test fixtures and this task record (integration owner).
No real lock/profile/native/runtime activation changes belong to this slice.

## Installed local-run resource and startup binding

The next delivery completes a deployment transport, not generic Registry
readiness. Local profile compilation (defined in the scope correction below)
and both protected Git chains remain mandatory. No production resource can be emitted from
fixtures, caller contexts or repeated hashes.

Trust root: the installed wheel is a controlled build artifact, like installed
Python code. A sibling digest detects resource drift; it does NOT authenticate
an arbitrary replacement wheel or replace Git verification. Runtime does not
replay Git or accept a resource path, profile selector or environment snapshot.

The closed canonical JSON wire is installed-local-run-authority-v1, containing
exactly schema_version, current and predecessor. Each chain has exactly:
registry_set_sha256, authority_lock_sha256, narrative_raw_base64,
profile_raw_base64 and schema_raw_base64. All six encoded sources preserve the
exact locked bytes (strict canonical base64). Current profile/schema are
local-run; predecessor profile/schema are shadow. Keep both narrative sources:
their bytes must not be assumed identical. No serialized ready/accepted,
snapshot, entry, profile key or calibration references are allowed. Those values
are derived by the existing grammar decoders from the original raw sources.

The Kernel decoder strictly parses the resource; validates nonzero hashes;
re-decodes both narratives, shadow and local-run with raw schema hashes;
recomputes the timed-speech schema-closure digest; and verifies predecessor
version/source/Registry/lock identities. Its typed result describes content only.
It cannot certify build provenance or substitute for a committed calibration.
Errors are a dedicated ValueError subtype and never include source bytes/secrets.

The fixed installed reader uses importlib.resources beneath autocut_kernel at
_authority/local-run.json and _authority/local-run.sha256. It accepts no
arguments. Missing, mismatched or malformed files fail closed. No valid default
resource is checked in. A private pure decoder remains directly testable.
The explicit build/admin emitter calls the existing dual-chain builder and
accepted-record binder itself, rereads exact locked source bytes, emits canonical
resource bytes and their digest, and never accepts caller snapshot/context.
Ordinary wheel build must not connect to DB or infer profiles. Both root and
standalone Kernel wheel packaging must include only an explicitly prepared
resource; fixture output is confined to test temporary directories.

Move the existing source-to-accepted-record comparison into one Kernel owner,
preserving all producer/corpus/0-and-3 member/bound checks. The tools binder
delegates rather than duplicating it. The installed startup path uses that same
comparison with the real Store, then resolves the existing immutable timed-speech
bootstrap anchor and compares the ENTIRE returned entry to the installed entry.
Only then may worker reconstruction occur. It performs no bootstrap or Store
write. The independent admin path alone can construct the existing protected
bootstrap request after the same accepted-calibration comparison.

Do not wire the resource into standard composition until decoder, build emitter,
both wheel paths and anchor verification have tests. Keep the old explicit typed
test seam distinct from standard no-argument installed loading; it must not
become an HTTP/caller capability. Narrative/provider policy compatibility is a
separate required activation check, not implied by matching the ASR profile.

Test boundaries: synthetic Git chains and fake accepted Store readers prove
transport/closure only. Cover duplicate/extra fields, invalid base64, changed
sources/hashes/refs, whole-schema-vs-component digest substitution, absent
resource, entry substitution, and denial before worker recovery. Installed-wheel
checks must run without the checkout/tools/Git on import paths. Real Registry
sources and actual measured calibration remain outstanding.

Ownership: resource codec/fixed loader + focused tests (calibration_contract);
local profile source compiler + context builders/tests (calibration_migration); shared calibration binding,
packaging/composition integration and task records (root). Files may be assigned
only after interfaces are frozen; the independent reviewer writes none.

## Local profile compiler scope correction — 2026-08-26

This section supersedes earlier requirements in this task to compile all eight
generic Registry packs before calibration/local-only execution. The withdrawn
v2-production-system-contracts.md is not restored. The generic compiler and its
19-command completeness check remain unchanged for their own full Registry
scope; they are not the current Pipeline's executable command catalogue.

Current local authority consists of the closed narrative, shadow/local-run and
profile-schema sources, the existing protected timed-speech bootstrap writer,
and immutable calibration/profile anchors. Its identity must not masquerade as
generic Registry readiness. Define the domain-separated identity:

    canonical_json_hash({
      "schema_version": "local-profile-registry-v1",
      "profile_kind": "shadow_calibration_v1" | "local_run_v1",
      "sources": [
        {"role": "narrative", "sha256": SHA256(original narrative bytes)},
        {"role": "profile", "sha256": SHA256(original profile bytes)},
        {"role": "profile_schema", "sha256": SHA256(original schema bytes)}
      ]
    })

The kind must match the decoded profile state; roles are fixed and ordered.
A locked profile compiler verifies the complete A/B/C chain and lock-covered
bytes, then reads these three exact source roles with their expected classes.
Both profile context builders retain all grammar/identity/inheritance checks.
They return a dedicated LockedProfileCompilation with registry_sha256, not a
RegistrySet, ready flag or generic compilation result. Obsolete generic Registry
repository/root selectors are removed rather than silently ignored.

The installed resource codec recomputes this domain hash for each chain from
its exact raw sources. It rejects old generic hashes, swapped profile kinds or
another source set. Existing registry_set_sha256 / registry_snapshot_sha256
fields in the timed-speech snapshot and calibration record retain their wire
names but carry this local, purpose-specific identity on this path. They confer
no full command-matrix completeness or publication permission. Never reinterpret
an existing accepted anchor under the new identity: a different hash is a
different immutable profile/record, not an in-place migration.

This changes source compilation scope, not media safety or calibrated evidence:
real model/service identity checks, independent measured calibration, exact
accepted member references and whole bootstrap-entry comparison still apply.
There is no zero/default profile, fake readiness or legacy implementation reuse.
Publishing the actual new inventory/lock must remove references to the withdrawn
document; existing historic locks remain historical, not deployment authority.

Delivery order: (1) domain identity + fixed resource codec, (2) replace accidental
generic compiler dependency and share the calibration comparison, (3) explicit
emitter/wheel packaging, (4) installed bootstrap and provider-compatible HTTP
composition, (5) real calibration and local Pipeline verification.

## HTTP activation and remote execution boundary — 2026-08-26

The user has assigned this workstation code development, automated tests and
review only. Native model inference, real calibration and the complete drama
Pipeline are to run on the remote desktop. Synthetic tests do not satisfy those
remaining acceptance criteria. Do not start local database/model services for
this slice.

Standard HTTP composition has no authority_snapshot argument. With no Pipeline
configuration it remains disabled; partial configuration still fails clearly.
An enabled runtime reads only the fixed installed local-run resource. Before
worker reconstruction, it validates the accepted calibration anchor, resolves
the immutable bootstrap profile and compares the entire installed entry. The
separate admin bootstrap uses the existing protected Command; HTTP never calls it.

The currently registered HTTP stages are source_prep, vlm and media_preflight.
Their actual executable policies, not merely a typed profile object, must match
the installed source. Extract a stable prompt-template digest from the exact
static text used by build_vlm_prompt, and a stable sampling-policy digest from
the same algorithm/certificate/encoding/sample_count mapping used by
IdentitySourceWindowBuilder. Keep all existing per-window prompt and
selected-indices hashes unchanged: they identify different, dynamic inputs.

Define parser_contract_sha256 as a versioned, fixed installed implementation
bundle digest owned by Kernel, not as a duplicate response-schema hash or a
strategy-name hash. Its enumerated source members and bounded package-resource
reads must be documented and tested independently. This binds the parser code
used by this release; it is not a formal proof or authentication of a replaced
wheel. The controlled installed wheel remains the code trust boundary.

One VLM compatibility checker compares provider, model, adapter, prompt and
parser versions plus prompt/template/schema/parser/request/parse/retry/sampling
digests. Use it at composition and again on each persisted execution profile
before VLM dispatch or reconciliation. Never rewrite an old run to current
defaults. Persisted source windows must also retain the expected dynamic
sampling identity; a matching deployment default alone cannot certify old input.

Coverage/dependency/conflict policies belong to Stage 1, which is not registered
in the current three-stage HTTP plan. Retain their locked source identities
without claiming they were evaluated here. Stage 1 activation must define and
compare its actual policy owners before registering its executor. This explicit
scope does not waive any check for code that is executed now.

Regression tests must demonstrate startup failure before worker recovery on
missing/calibration/profile mismatch, no provider call for an incompatible
persisted run, and unchanged prompt/window request identities after extraction.
Test-only loader/Store substitutes must be explicit; no production default or
environment profile bypass is added.

### Executed identity and unit closure

The parser implementation bundle is the ordered set
media/root_evidence.py, media/types.py, vlm/models.py, vlm/parser.py and
vlm/window.py under the installed Kernel package. Each exact raw member hash
is included with its path in canonical material with schema_version
vlm-parser-implementation-contract-v1 and parser_strategy_version
strict-semantic-pack-v3. Reads are bounded to 4 MiB per file, with no cache or
caller path. Comments also change this identity. It binds this fixed parser
dependency set, not stdlib, bytecode substitution or arbitrary runtime mutation.

The normal media policy's timed_speech_calibration_sha256 identifies the accepted
aggregate CalibrationRecord, while each producer calibration identifies its
distinct ASR or VAD child. Match provider/service/tool/model identities and
producer metadata, detector recomputation, timing policies and exact positive
bounds. Bound conversion must satisfy the exact rational equality between
microseconds and the source clock; rounding cannot substitute another bound.
The service request-size value must equal the installed native max_request_bytes,
as required by the existing native service protocol.

Keep PipelineExecutionProfile v5: no independent parser replay bypass was found.
Current narrative must inherit the shadow narrative; changing executable parser,
template, timing or native identity changes shadow identity and its independently
accepted aggregate record. Persisted media policy already names that aggregate.
Both resumed VLM and resumed media compare it to the installed aggregate before
any command/provider work. Separately, the existing media Kernel claim binds
the installed Registry snapshot. Do not add a duplicate execution-profile version
without an evidenced missing identity.

The installed media adapter validates the full accepted/profile binding before
delegating to the existing Kernel Command's StoreAnchored resolver with the exact
same snapshot. It does not weaken that Command or introduce a private Store write.
