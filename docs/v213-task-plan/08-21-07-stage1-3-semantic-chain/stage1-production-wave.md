# Stage 1 production preparation — 2026-08-26

Task: 07-stage1-3-semantic-chain. Branch: feat/v213-contract-codegen.
This checkpoint does not certify Stage 1 or enable another HTTP stage.

## Current evidence and delivery order

The exact committed Source/Window/VLM reader is implemented. The existing
semantic_chain/stage1.py is an inactive prototype, not the production compiler:
it has four local rules instead of the KC evaluators, incomplete graph/proof
models, non-orthogonal coverage and a mutable serialization result. Do not wrap
it as an accepted BuildNarrativeGraph result or export its test authority factory.

Implement in parallel:
1. Shared generation persistence keyed by explicit execution kind.
2. Strict, untrusted Stage 1 draft decoding over committed VLM semantics.
Then replace the prototype with the actual graph/coverage/dependency compiler,
add BuildNarrativeGraph and its exact eight-member output reader, and only then
activate it in Runtime. Stage 2/3, editing and Render/QC remain required work.

## Execution kind (existing design correction 4)

CommandClaim requires keyword-only execution_kind=deterministic|generation;
there is no Python/SQL default and no inference from a new command's name.
The durable column is immutable and participates in idempotency equality.
Generation reserve/retry/dispatch/reconcile/commit and deferred SQL integrity
use this kind. Generic success/rejection cannot terminalize a generation slot.
Existing dedicated bootstrap, calibration, batch and finalizer owners retain
their separate guards; the enum does not grant access to those APIs.

Migration 0018 explicitly classifies historical GenerateVlmEvidenceCommand
rows as generation and other existing rows as deterministic, after verifying
that all existing generation attempts have the supported historical owner.
This one-time historical backfill is not the runtime dispatch rule. Existing
migration bytes are unchanged. Every current CommandClaim caller is migrated
in the same wave; new commands declare their kind themselves.

Deployment is coupled: stop workers, verify the database has migrations through
0017, apply 0018 once, then start the matching new code. Old code cannot insert
slots after this no-default migration; a code-only rollback is not sufficient.
Fresh databases apply the ordered migration chain. Do not rerun the historical
SQL files against an existing database. No migration is applied locally here.

## Stage 1 generation draft boundary

The draft root is exactly schema_version, input_binding_sha256, beats,
obligations, story_threads and merge_proposals. Its explicit limits cover bytes,
collections, references and text. Strict JSON rejects duplicate/unknown keys,
nonfinite numbers, floats, malformed UTF-8, unknown enums and forbidden inputs.
The semantic prompt projection also has an explicit byte budget: an oversized
projection is rejected intact, never silently truncated. Policy values have a
canonical hash for the future frozen generation request.

Within the exact bound semantic input, evidence references name a committed
window, a global entity/fact/event ID and its object type. The input binding
includes exact Source/grant, aggregate and child owner/content/request identities;
these short draft references are not authoritative cross-Artifact output refs.
Prompt inputs expose only committed entity/fact/event/summary/continuity data.

Draft-local IDs close beat/obligation/thread references. Merge proposals require
multiple entities and factual/event support but remain proposals: decoding never
accepts an identity merge, grants coverage, manufactures RuleResult or Admission,
or constructs the old decoder authority. Empty proposals are structurally legal,
not proof that coverage obligations are satisfied. Values are immutable; mappings
returned to callers are fresh containers.

## Ownership and verification

- calibration_migration: Store models/postgres, migration 0018 and focused tests.
- calibration_contract: stage1_draft.py and its dedicated tests.
- root: existing claim/SQL test callers, task records and integration.
- review_calibration_migration: read-only independent review.

No model, database or complete Pipeline is run on this workstation. Unit tests,
static migration checks and written remote PostgreSQL regressions are distinct
evidence. PostgreSQL migration/restart/replay acceptance and real model execution
remain on the remote desktop. No external publication is enabled.

## Before the production compiler

Resolve the source entity/Graph owner model explicitly: VLM object, location and
screen_text_source cannot be silently converted to character or have their facts
dropped. Clarify character_state projection and acyclic precommit references.
Retain normative orthogonal coverage and Ledger-owned taint seeds. These are not
requirements to relax; the draft/persistence slices do not claim to resolve them.
