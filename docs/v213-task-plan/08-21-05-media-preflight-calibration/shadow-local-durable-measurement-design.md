# Shadow-local durable measurement design

Status: frozen implementation design on 2026-08-26. This is a sibling of the
complete-source shadow calibration route, not a change to it and not a local
profile activation design.

## 1. Purpose and non-goals

The local FunASR/FSMN route already produces a pre-calibration profile, an
ordered local window corpus, original response bytes and an independently
replayed projection. This design makes those measurements durable and
recoverable exactly once per command identity.

It does **not** create a `CalibrationRecord`, authority anchor, Registry update,
installed profile or publish permission. The old complete-source command,
protocol, migrations `0016`/`0017`, DTOs and readers retain their existing
meaning and must not be widened.

## 2. Identity and input closure

`MeasureShadowLocalCalibrationCommand@1` is deterministic and uses the fixed
protocol `shadow-local-calibration-measurement-v1`.

```text
ShadowLocalServiceProfile + ordered Manifest + source bindings + fixed limits
  -> Store-command canonical payload / request hash
  -> one shadow Job and command slot
  -> versioned durable attempt/member journal
  -> two-member succeeded ArtifactSet, or one terminal Receipt only
```

The request contains only closed values:

- the entire pre-calibration `ShadowLocalServiceProfile`;
- a nonempty ordered `ShadowLocalMeasurementManifest` whose member is
  `(ordinal, exact case, exact request)`;
- one same-order source binding per member: original succeeded source `Job` and
  exact source `BlobRef` identity;
- explicit materialization, plan, per-response and total-response limits; and
- a positive maximum attempt count. It limits recovery; it never permits an
  implicit native retry.

Before claim/dispatch, the compiler verifies all members, source owners,
BlobRef metadata, case/request derivation, profile/native/decoder/producer and
policy identity, uniqueness/ordering and all byte budgets. A source is allowed
to appear in multiple windows. The source blob is read under its original
owner; raw response blobs are instead claimed by the new measurement Job.

There are three non-interchangeable hash domains:

| Identity | Owner | Encoding |
| --- | --- | --- |
| media case/request/manifest/results hash | media domain | canonical JSON with `ensure_ascii=True` |
| command and persisted Artifact payload hash | Store | `canonical_payload_hash` with `ensure_ascii=False` |
| provider response hash | response bytes | SHA-256 of the unmodified bytes |

No code may compare or substitute hashes across these rows. Unicode is a
required regression case.

## 3. Durable plan and state machine

The Store receives a typed local plan whose immutable member identity includes
ordinal, case/request hashes and canonical values, original source owner/BlobRef
identity, source blob reference hash, per-member response limit and all relevant
profile/policy/decoder identities. Its command claim uses the Store request hash,
`Job("shadow-local:<request summary>", "shadow")`, and idempotency key
`shadow-local-measurement:<request summary>`.

Each local attempt is isolated in `runtime.shadow_local_calibration_measurement_*`
tables added by migration `0023`; it never shares `0016` tables. Attempt states
are `prepared -> collecting -> ready -> committed`, with `indeterminate` for an
unknown native outcome and terminal `denied` only for independently proven
invalid raw evidence. Member states are `pending -> invoking -> staged`, plus
`indeterminate`, `not_started` (a request-bound BUSY proof established no native
dispatch), or `rejected`. All transition updates use exact version CAS.

The winning worker first persists `invoking` and lease, then makes one native
dispatch. There is no network exactly-once claim. On an expired invocation,
recovery marks the member/attempt indeterminate; it never changes the member
back to pending. A request-bound BUSY proof similarly becomes `not_started`,
not an invented unknown outcome. A retry requires a durable explicit
authorization and creates one contiguous successor under the same command
slot/plan. It inherits already staged members, preserves later `pending` members
for their first dispatch, and attempts only the authorized unknown/not-started
member(s) again. Thus a source response is never sent twice merely because a
later member failed. The retry budget and authorization bind predecessor
version, plan/member identities and next ordinal.

Raw bytes plus raw BlobRef metadata, independently replayed measurement
evidence and member `staged` state commit in a single Store transaction. A
restart after staging reads the staged bytes and never calls the provider again.
If the response is fully received and the Kernel's independent decoder proves it
invalid, the command makes a compare-and-swap terminal denial and produces no
ArtifactSet. It may retain previously staged blob audit data but never turns it
into a successful set. A denial is legal only when no other member remains
`invoking`/unknown; the normal collector dispatches serially, so this is the
expected deterministic branch. Timeout, broken transport, uncertain provider
process, unknown stage/finalize commit and lease loss are not invalid evidence;
they remain running/indeterminate and are reconciled by exact reads, never
rewritten as denial.

The local command is reserved from generic claim/success/rejection code. SQL
and Store guards ensure that only the local journal finalizer can commit its two
artifact members, and generic paths cannot fabricate a success or a rejection
that bypasses the recovery ledger.

## 4. Port error boundary

The current application HTTP adapter discards the bytes when a `200` response
fails decode. That is insufficient: Kernel cannot infer whether the remote call
was completed and demonstrably invalid or never conclusively completed.

The local-measurement port therefore adds a Kernel-owned invalid-response value
carrying the exact response bytes and locked request identity. The command
performs its own bounded decode/replay; only this branch can make a terminal
denial. Timeouts, connection errors, unknown non-proof status and post-dispatch
exceptions use distinct unavailable/unknown outcomes. BUSY is accepted only
after independently decoding a request-bound proof that no dispatch occurred;
it stages a `not_started` proof and does not itself authorize a successor.

## 5. Succeeded set and exact reader

Only a completed attempt finalizes exactly two revision-1 members in one
ArtifactSet, under scope `autocut_calibration / shadow_local_run / <request
summary>`:

1. `shadow_local_measurement_manifest`, logical id
   `shadow-local-measurement:<summary>:manifest`;
2. `shadow_local_measurement_results`, logical id
   `shadow-local-measurement:<summary>:results`.

The durable manifest payload contains the full request and Store request hash.
The durable results payload binds the manifest artifact content hash, pure media
results mapping and ordered raw BlobRefs `(ordinal, case hash, request hash,
object id, content hash, length, media type)`. It contains no `pass`, accepted
bound, record, activation or Registry field. The artifact-hash link is separate
from the pure manifest hash embedded within local results.

The exact reader verifies command/job/slot/Receipt/set/scope/type/logical-id/
revision, journal committed state, immutable plan/member identities, exact raw
BlobRef claims by the measurement Job, ordinal coverage and all byte budgets
before any raw read. It then reads every raw blob under the measurement owner,
verifies byte hash/length/media type and independently rebuilds
`ShadowLocalMeasurementResults` and its unaccepted report. It rejects wrong
owner, substituted/rehashed raw data, mixed attempts, omitted/reordered members
and producer-supplied projections.

## 6. Required tests and implementation order

1. Define command/plan/port invalid-response types and test fully closed input
   derivation, including Chinese/Unicode values and the three hash domains.
2. Add sibling Store DTOs plus migration `0023` and static migration tests:
   reserved-command generic-path rejects, immutable plan/member fields, legal
   CAS transitions, exact blob claim and no authority writes.
3. Implement journal methods and command fake-store recovery tests: concurrent
   lease, stage replay, first-member unknown with later pending, explicit
   successor, invalid second response after a staged first response, and
   uncertain finalize reconciliation.
4. Implement exact reader tests: wrong job/slot/receipt/set/scope/ordinal,
   foreign source/raw owner, response budget before reads, two members from
   different clocks/time bases, all hash domains and no activation side effect.
5. Run PostgreSQL CAS/restart tests on the desktop database before classifying
   this as real local measurement; Mac fake tests alone do not satisfy that
   acceptance.

## 7. Implementation freeze: command and exact reader

The command request compiles the existing pre-calibration service profile,
ordered pure manifest, exact original source owner/BlobRef binding and explicit
limits into the reviewed Store plan. Store canonical payload hashing owns the
request/Artifact identity; embedded media case/request/results retain their
media-domain identity. `ShadowLocalMeasurementResults` contains no BlobRef, so
the reader must join the exact committed local journal to obtain the ordered raw
BlobRefs; reading two JSON artifacts alone is insufficient proof of ownership.

Generic `claim_command`, generic success and generic rejection must reserve the
local command exactly as they reserve the old full-source command. The dedicated
finalizer writes only the two local artifacts; it cannot call calibration-record,
Registry, installed-profile or publish APIs. The reader first closes every
job/slot/receipt/set/journal/member/blob/budget identity, then reads raw bytes
under the measurement owner and independently replays the pure results/report.

An `InvalidResponseError` is only a byte carrier: the command compares its
request with the locked request and replays the bytes. Valid bytes stage; only a
failed independent replay is an invalid-raw denial. Transport uncertainty and
commit uncertainty never become a denial. A request-bound BUSY proof persists
as `not_started`, and an explicit successor authorization is still required.
