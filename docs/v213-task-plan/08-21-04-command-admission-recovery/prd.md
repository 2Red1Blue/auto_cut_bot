# Command Admission and Recovery

## Goal

Implement the shared v2.1.3 command authority kernel: a single Dispatcher,
immutable receipts and admissions, and a Recovery Controller whose idempotency,
budget and crash behavior is identical for Pipeline and Agent-Native runtimes.

## Confirmed authority and scope

- Source authority admission is clean commit
  `baf667f797ac7d4eb34e48caad8047fb07433c9c` on
  `docs/v213-source-span-erratum`, with total-contract raw SHA-256
  `c34af7451919ad9a895644b40136062834b7ba9e857139f10b61f7dc51be67e9`.
  This admits only the current baseline: any independently accepted F1 or
  later authority amendment supersedes it and invalidates this task's prior
  plan, context and predecessor admission until re-verified.
- Task03 supplies only generic immutable ArtifactSet/head-CAS storage. This
  task supplies execution rows, profiles and composition; it must not re-write
  generic Store semantics.
- Runtime adapters can submit Commands and read immutable results only. They
  cannot write slots, receipts, Admission, RecoveryLedger or projections.
- A new Recovery fingerprint has two closed paths: sufficient budget reserves
  then executes; zero/insufficient budget atomically records one exhausted
  Attempt and Ledger entry with zero debit and no executable slot.
- No legacy package import, legacy ArtifactBus/Stage/Policy object, mutable
  budget counter, hidden default, or unregistered `reanalysis` kind is in
  scope.

## Requirements

1. Dispatcher re-reads the complete RegistrySet, validates the closed profile,
   exact refs, scope, capability and derived idempotency identity before any
   Handler runs.
2. Command slots provide lifecycle/idempotency only. Receipt and normalized
   execution rows use exact paired Artifact/ArtifactSet references and the
   five authoritative effect phases.
3. Admission is a pure evidence re-evaluation over pending business members;
   handlers and runtimes cannot supply or override its RuleResult/action.
4. Recovery fingerprint is exactly the §9.1 RFC 8785 tuple and its ordered
   arrays are independently validated from `subject_refs` and prior Admission
   RuleResults.
5. Every ledger transition uses exact-head CAS. A stale writer retries from
   the current immutable ledger; it never reuses a stale last-budget result.
6. All durable transitions needed for each profile are one PostgreSQL
   transaction. Crashes are resumed from receipts/heads, never inferred from
   process memory or a projection.

## Acceptance criteria

- [ ] AC1: Profile/schema rejection makes zero Handler calls for missing,
  extra, incompatible or stale inputs, derived-key mismatch, invalid scope or
  capability.
- [ ] AC2: Same command key/request/profile creates one slot/receipt; same key
  with a different request/profile conflicts; every lifecycle/effect state
  invariant and paired exact-ref FK is PostgreSQL-enforced.
- [ ] AC3: The evaluator rejects self-declared pass, emits only registered
  RuleResults/actions and atomically commits its immutable Admission with the
  business set.
- [ ] AC4: Concurrent same Recovery fingerprint produces one debit and one
  reservation/outcome. Concurrent different fingerprints for the last budget
  let only one reserve; every loser re-reads current head.
- [ ] AC5: G-REPAIR-004/005/006 are executable: insufficient cost produces
  Attempt→Ledger exhausted evidence, retry returns it, and concurrent different
  zero-budget fingerprints serialize by head CAS without debit or slots.
- [ ] AC6: Fault injection before/after reserve commit, slot creation, handler
  completion, receipt commit and ledger finalize yields no duplicate debit,
  model call, business write or unauthorized Command.
- [ ] AC7: Pipeline and Agent-Native adapters replay the same input to equal
  exact receipt/Admission/Ledger/outcome refs and balances; direct Runtime
  Store/slot/projection writes are denied and observable.

## Out of scope

- Media preflight, Stage 1–4 semantics, render/QC, external publication and
  real provider invocation implementation.
- New Recovery kinds, changing the frozen contract, legacy-code migration,
  alternative databases or a mutable recovery-budget table.

## Risks and entry conditions

- Task01 must expose the required closed profiles/registry entries, and Task03
  must expose an approved transaction-composition/UoW extension plus PostgreSQL
  integration harness. If either is absent, this task remains planning/blocked
  at that boundary rather than duplicating it.
- Every implementation change must pass the import firewall and Reuse Ledger;
  no old code may be imported as a shortcut.
