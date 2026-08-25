# Dual Runtime Conformance Implementation Plan

## Entry Gate — No Code Before All Pass

1. Validate the task and parent link. Treat `implement.jsonl` and `check.jsonl`
   only as context-source declarations: validate that each real source exists,
   but do not treat either list or its reason strings as a doc12
   `TaskSnapshot`, admission receipt, check result or pass oracle.
2. Run the approved Context Loader before any implementation Agent starts. It
   must read every direct source to UTF-8 EOF and freeze a generated
   `TaskSnapshot` containing full-file byte length/hash and EOF evidence,
   selected section anchors and exact ranges with slice hashes, the hash-bound
   reference closure, exact `feat/v213-contract-codegen` predecessor,
   authority/Schema/Registry/Kernel identities, allowed paths, and
   gate-bundle/toolchain-lock/Supervisor-contract hashes. Reject truncation,
   unstable anchors, missing closure members or stale hashes. If the complete
   closure exceeds the approved context budget, split the task or narrow the
   declared source/section scope and regenerate; never inject the whole corpus
   or substitute an automatic summary.
3. Require an accepted Authority Change that removes the tracked unresolved
   dual-runtime binding and supplies the detailed closed conformance trace,
   projection and receipt schemas. Verify generated outputs and authority lock;
   Task09 does not modify those protected files.
4. Verify accepted predecessor commits for Store/CAS, Command/Admission/
   Recovery, semantic/media/physical-edit, render/QC/local release and the
   concrete durable Pipeline worker. Run their required PostgreSQL checks.
5. Re-census the current integration tree. Confirm that the historical Agent
   Runtime files are absent or current and bind the chosen reimplementation
   scope explicitly. Do not cherry-pick or copy deleted code as authority.
6. Verify both runtime compositions use one exact Kernel build and the same
   test/shadow execution profile. Confirm publication credentials/capability/
   routes are absent and install a deny-on-call Platform canary.
7. Verify a disposable PostgreSQL DSN is available and production migrations
   can create two isolated schemas. Deny entry if tests would fall back to
   SQLite, private schemas or filesystem truth.

Any failed item leaves the task in planning. Repair the owning predecessor or
open an Authority Change; do not implement a local compatibility path.

## Wave 1 — Shared Projection and Comparator

Owner: `packages/autocut-kernel/src/autocut_kernel/runtime_conformance/**` and
`tests/runtime_conformance/unit/**` only. Unit-local builders/helpers remain
under `unit/`; this wave owns no root-level conformance test file and no
fixture/PostgreSQL/integration/support subtree.

1. Add closed generated-model consumers for runtime observation trace,
   canonical command/final projection, exclusion policy and detailed receipt.
   Do not hand-maintain a second schema.
2. Implement the read-only Store projector from committed input set through
   exact Command/Receipt/ArtifactSet/head/Admission/Recovery relations.
3. Implement deterministic registered ordering, reachability/completeness
   validation and RFC 8785/JCS hashing.
4. Implement the closed exclusion projection and comparison result with a
   single `allow|deny` decision. Unknown or incomplete data raises a typed deny,
   never a partial projection.
5. Add fixture tests for strict decoding, randomized exclusions, all business
   one-bit mismatches, duplicate/missing/uncommitted facts and false runtime
   success reports.

Rollback point: the wave is additive and may be removed before adapters consume
it. Committed Store facts are read-only and unchanged.

## Wave 2 — Pipeline HTTP Runtime Adapter

Owner: the listed Pipeline runtime modules and only
`tests/pipeline/runtime_adapter/**`; no Agent, Wave 1 unit or Wave 4
fixture/PostgreSQL/integration/support files.

1. Replace every business stage callback with the public typed Command Gateway
   port. Keep run/outbox/lease state explicitly transport-only.
2. Make stage selection read committed snapshot, Admission next action and the
   frozen completion policy before each proposal.
3. Reconcile existing Command identities on restart, replay, ack loss and
   `indeterminate`; prohibit replacement invocation and transport-derived
   success.
4. Emit the closed observation events around Gateway calls without using them
   as business success evidence.
5. Add unit/PostgreSQL tests for replay conflict, restart reconstruction, stale
   lease, indeterminate reconciliation and runtime-role business DML denial.
6. Run an import/dependency scan proving the Pipeline modules do not import
   Store writers, Admission evaluator, physical editors, FFmpeg/Renderer,
   Provider/Platform/publication or legacy modules.

Rollback point: disable the new adapter composition and retain the durable
control-plane rows; do not delete Kernel receipts/artifacts.

## Wave 3 — Restricted cut_bot/nanobot Adapter

Owner: `auto_cut_bot/autocut_agent_runtime/**`, one dedicated v2.1.3 tool/profile
composition module named in the accepted manifest, and only
`tests/autocut_agent_runtime/**`; no Pipeline or conformance-harness files.

1. Define closed query/proposal/explanation DTOs using exact committed refs and
   registered semantic intent. Reject dict/path/PTS/tick/Recipe/QC/render/
   provider/platform fields before Gateway invocation.
2. Compose the adapter with a prebuilt public Gateway, Query Gateway and
   Recovery Catalog only. It receives no Store, internal service locator or
   effect ports.
3. Build a dedicated exact-allowlist nanobot `ToolRegistry`. Prove shell,
   filesystem mutation, generic network/MCP, default legacy pipeline,
   database, render and publication tools are absent from this profile.
4. Map Gateway results to closed Agent responses and append observation events.
   Natural-language explanation remains non-authoritative and outside business
   comparison.
5. Add import-boundary, exact tool-set, prompt injection, illegal intent,
   terminal mapping, restart and existing-receipt tests.
6. Run dependency and credential scans; assert no provider/platform credential
   is reachable from the Agent composition.

Rollback point: unregister/disable only the dedicated v2.1.3 Agent profile. The
general nanobot product remains separate and no committed Kernel fact is
rewritten.

## Wave 4 — Paired Conformance Harness

Owner: only `tests/runtime_conformance/fixtures/**`,
`tests/runtime_conformance/postgres/**`,
`tests/runtime_conformance/integration/**` and
`tests/runtime_conformance/support/**`. It owns no
`tests/runtime_conformance/unit/**`, root-level conformance test file or
production adapter/module.

1. Build deterministic seeds containing identical committed IDs/refs, input
   ArtifactSet, frozen clock, policy and strategy. Load them through production
   migrations into two isolated PostgreSQL schemas.
2. Drive one seed through Pipeline and one through Agent using the same Kernel
   composition. Project only committed Store facts and compare canonical JCS
   bytes/hashes.
3. Cover the full matrix:
   - happy Recipe + local render + structural/media/editorial/local-release QC;
   - Admission/precondition denial;
   - replay and changed-request conflict;
   - crash before/after claim, effect result and Receipt/ArtifactSet commit;
   - commit-ack loss, stale lease/fence and restart;
   - provider `indeterminate` and same-Attempt reconcile;
   - recovery reserve/finalize, crash, contention and exhaustion;
   - Pipeline↔Agent switch at every Command boundary;
   - direct runtime DML denial and publication canary zero calls.
4. Persist raw trace hashes, both canonical projection hashes, comparison
   sections and evidence refs into the detailed receipt. Validate it against
   the generated schema and then feed its hash to the generic task-gate
   runtime-conformance predicate.
5. Repeat a fixture with randomized permitted transport fields and prove the
   comparison hash is unchanged. Mutate each authority-bearing class and prove
   deterministic deny.

Rollback point: test schemas and local fixture outputs are disposable. Remove
them through the harness cleanup path; no external effect exists to roll back.

## Wave 5 — Independent Quality Gate

The checker is a separate read-only run and owns no production or oracle file.
It must:

1. Recompute authority/context/candidate/toolchain hashes and reject any Wave 0
   protected-path change in the ordinary Task09 diff. Revalidate the
   machine-generated context manifest, including full-file EOF/hash, selected
   range/slice hashes, reference closure and gate/toolchain/Supervisor hashes;
   never infer a pass from either JSONL source list.
2. Run task validation, import firewall, Reuse Ledger, format/lint/type checks,
   focused unit tests and the complete disposable PostgreSQL matrix.
3. Inspect the runtime capability graph and database grants; verify both
   adapters terminate at the same public Gateway and cannot reach Store/effect
   internals.
4. Recompute sample projections/receipts from raw committed Store state, not
   saved expected outputs or runtime claims.
5. Map evidence to AC1-AC9 and emit `allow|repair|deny` for the exact candidate
   tree. A stale/missing PostgreSQL case, skipped publication canary or any
   business mismatch is `repair`/`deny`, never N/A. Every `repair` names one
   unique original owner and a minimal path inside that owner's frozen
   boundary. Cross-boundary problems become ordered separate findings; the
   checker and Wave 4 may not repair another owner's files.

## Required Validation Commands

Resolve exact commands from the accepted predecessor at task start; record
their versions and output hashes in `CheckReport`. The minimum check classes
are:

```text
python3 .trellis/scripts/task.py validate 08-21-09-dual-runtime-conformance
authority-lock / generated-schema / RegistrySet verification
Task09 diff allowlist and protected-path verification
Context Loader/TaskSnapshot full-file EOF+hash, selected range+slice,
reference-closure and gate/toolchain/Supervisor-hash verification
AST import firewall + dependency graph + isolated Kernel wheel smoke
Ruff + BasedPyright for owned Python packages
focused runtime-conformance unit tests
Pipeline adapter unit/PostgreSQL tests
Agent exact-tool-profile and import-boundary tests
complete paired real-PostgreSQL conformance/fault matrix
candidate-tree Supervisor check against AC1-AC9
```

If a documented command no longer exists, do not silently substitute a weaker
check. Replan and bind the replacement toolchain first.

## File Ownership and Scheduling

- Wave 0 is an independent authority task and may not overlap Task09.
- Waves 2 and 3 may run in parallel only after Wave 1 is accepted because their
  production and test ownership does not overlap: Wave 2 owns only its listed
  Pipeline modules plus `tests/pipeline/runtime_adapter/**`, and Wave 3 owns
  only its manifest-listed Agent modules plus `tests/autocut_agent_runtime/**`.
- Wave 1 exclusively owns `tests/runtime_conformance/unit/**`. Wave 4 starts
  after both adapters are accepted and exclusively owns the `fixtures/`,
  `postgres/`, `integration/` and `support/` subtrees. Neither owns a
  root-level file below `tests/runtime_conformance/`; if one becomes necessary,
  stop and replan it to one owner before creation.
- Wave 4 reports adapter/comparator defects back to their unique original Wave
  1, 2 or 3 owner; it does not patch those files. A finding spanning ownership
  boundaries is split into ordered findings rather than assigned jointly.
- Wave 5 is read-only. A repair returns to exactly one original owner recorded
  in the TaskSnapshot path-owner map and invalidates receipts for the changed
  candidate tree. Ownership is never transferred merely because a later wave
  discovered the defect.
- At no point may two agents edit the same file or may an implementer edit the
  protected authority, exclusion oracle or expected conformance receipt.

## Stop Conditions

- Detailed dual-runtime source/schema is still unresolved or differs from the
  generated models.
- Context Loader output is absent/stale, does not prove full-file EOF/hash,
  selected range/slice hashes or complete reference closure, or does not bind
  the exact gate/toolchain/Supervisor hashes. An over-budget closure stops for
  task decomposition; full-corpus injection and automatic summaries are not
  fallbacks.
- Any predecessor is missing and passing requires Task09 to implement it.
- Pipeline and Agent cannot use one exact Kernel/authority/profile/input set.
- A Runtime needs Store/UoW, Recipe/QC constructors, FFmpeg/Provider/Platform,
  generic shell/network or publication capability.
- Equality requires an unregistered exclusion or ad-hoc normalization.
- A crash/replay/indeterminate case creates a second invocation, budget debit,
  model/render effect or visible output.
- Publication capability/credential/route is reachable or any external call is
  observed.
- Real PostgreSQL evidence is unavailable, skipped or replaced by SQLite.
