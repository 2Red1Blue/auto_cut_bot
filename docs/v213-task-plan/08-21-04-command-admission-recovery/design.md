# Task04 Design — Command, Admission and Recovery Authority

## Boundary

`autocut_kernel.execution` owns deterministic command admission and recovery
composition. It depends on Task01 public contracts and Task03 Store/UoW ports;
it does not import a Runtime, `autocut_legacy_bridge`, or a legacy package.
Pipeline and Agent-Native provide the same closed Command request through the
Dispatcher and receive immutable result refs only.

## Dispatcher and receipt flow

1. Resolve one complete RegistrySet and `CommandContractProfile`; validate raw
   request shape, principal capability, exact current/precondition refs, scope,
   Policy refs and derived invocation/slot identity.
2. Derive the canonical lifecycle key. Under PostgreSQL transaction and slot
   fencing, either return/reconcile the exact existing request or create the
   unique slot and first immutable Receipt/ReceiptRegistry revision.
3. A registered handler receives resolved immutable refs and a UoW staging
   interface, never a Runtime writer. It stages business members only.
4. The pure evaluator re-reads staged evidence and emits RuleResults plus an
   immutable Admission. The profile atomically writes the committed Set,
   paired exact references, succeeding Receipt revision and slot transition.

`execution.command_slots` is an idempotency/lifecycle projection. Receipt
Artifacts and their Registry are authority evidence; normalized receipt rows
must be exact-ref mirrors and have deferred trigger checks for profile/lifecycle
relations that cannot be expressed in a row CHECK.

## Recovery state machine

The Controller first validates prior Admission, registry-allowed kind/scope,
strategy, parameters and the exact 9-field RFC 8785 fingerprint. It rejects
noncanonical ordered arrays rather than repairing them.

```text
current exact Ledger head
  ├─ fingerprint exists → return existing reservation/outcome
  ├─ sufficient budget → Ledger reserved revision + debit + CAS
  │                       → recovery slot/receipt → handler
  │                       → business Set + Admission → Attempt → Ledger finalize
  └─ zero/insufficient budget → exhausted Attempt
                                  → next Ledger exhausted entry (exact Attempt ref)
                                  → exact-head CAS in one no-business-side-effect Set
```

The exhausted branch writes no reservation, Command/Generation slot, receipt
or outbox and debits neither attempts nor cost. Its Attempt is before, and
only referenced by, the resulting Ledger revision. Both branches retry from
the current head after a CAS conflict; an already exhausted fingerprint is
returned without another revision.

## Provider transport retry boundary

Provider transport retry is a bounded sub-state of one generation Command,
not a substitute for Admission-driven semantic Recovery. One CommandSlot may
own an ordered one-to-many GenerationAttempt chain; every Attempt authorizes at
most one Provider create. A frozen GenerationRetryPolicy controls the maximum
count (the first test profile uses three total Attempts), and each ordinal uses
a distinct deterministic provider idempotency identity.

Only a Provider-confirmed terminal `retryable` failure may advance to the next
ordinal. `repairable` output failures stop for a registered repair strategy,
`nonretryable` failures terminalize immediately, and `indeterminate` always
reconciles the same Attempt. HTTP trace IDs are not provider response IDs.

The Store owns a dispatch lease/token so a second worker cannot reconcile or
mutate an in-flight create. After expiry it may only claim reconciliation of
the original Attempt. Intermediate retryable failures do not create terminal
Command Receipts. Success or final failure commits the Receipt and a normalized
Receipt-to-Attempt relation covering the complete ordered chain in the same
transaction; generic Command rejection is not allowed to bypass this relation
for generation Commands.

The backoff schedule is authority data, not an advisory caller delay. The
Store re-derives the ordinal-specific delay from immutable request bytes,
checks the canonical policy hash, and persists immutable
`retry_backoff_seconds` plus `not_before_at`. Database dispatch is unavailable
before that timestamp. The registered 20-minute generation lease covers file
preparation and streamed response time; Provider configurations whose upload
and response bounds do not fit that lease are invalid.

Files preparation never masquerades as Responses reconciliation. A known file
whose processing query failed transiently may use a new retryable Attempt and
the same durable file cache identity. An upload with unknown acceptance is
repairable/quarantined and is never blindly uploaded again. Responses retrieve
429/5xx stays indeterminate on the same response ID, while deterministic
400/401/403/404/409/422 terminalize as nonretryable. A terminal
`response.failed` without explicit registered transient evidence is also
nonretryable.

## Persistence composition

Task04 migration extends `execution` only and references Task03 authority
tables with paired `(id, hash)` composite foreign keys. Required durable rows
are command slots, receipt mirror/history, and profile-approved outbox records.
There is no writable Admission or recovery-budget table. RecoveryLedger,
RecoveryAttempt, Admission and Receipt/Registry remain immutable Artifacts.

The UoW extension must permit one PostgreSQL transaction spanning Task03's
ArtifactSet/head write and Task04 execution rows. `SELECT … FOR UPDATE` plus
exact head revision/hash/fencing comparison establishes the linearization
point. Transaction loss is reconciled from the exact head and receipt, not a
client retry flag.

## Validation and rollback

All state-machine, FK, CAS race and crash tests run against PostgreSQL. Pure
fingerprint/profile/evaluator tests may run without it. A migration is
additive; rollback is application rollback while committed authority facts are
retained and new writers are stopped. No schema/code path is authorized until
Task01/03 entry conditions are verified.
