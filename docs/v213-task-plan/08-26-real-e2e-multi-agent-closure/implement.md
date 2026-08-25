# Implementation Plan

## Do-not-start gate

This parent cannot dispatch work until 08-25 is accepted and the Wave 0 child proves verified
calibration/profile/bootstrap. Before every wave, create the child task and its TaskSnapshot, confirm exact
predecessor commits/receipts, active ownership, Reuse Ledger declaration, repo-relative allowlist, toolchain and
network/publication deny configuration. A missing precondition stops at planning; no compatibility DTO, default
profile, legacy import or test-only authority substitute is allowed.

## Ordered execution

1. **W0 bootstrap** — accept governed calibration/profile/anchor closure. Run authority verifier and targeted
   adversarial review. Commit the child locally.
2. **W1 SourcePrep** — commit ordered source/window/proxy facts and immutable baseline ExecutionPartition before any
   provider invocation. Verify unselected 44 members have provider count zero. Commit.
3. **W2A/W2B evidence** — in independent worktrees, commit Ark semantic evidence and timed physical evidence.
   Timed child enforces one FunASR instance/inference permit, three queue permits, fixed budgets and original-attempt
   indeterminate recovery. Commit each leaf; do not edit shared composition/models/store/migrations/exports.
4. **W3 Blueprint** — consume both committed evidence families and emit Stage1–3 Blueprint. Commit.
5. **W4 Stage4** — consume W3 Blueprint plus physical evidence, recompute exact A/V Recipe/admission. Commit.
6. **W5 Render/QC** — consume admitted Recipe only; commit local Render/QC/LocalRelease and verified output reader.
   Assert publication port deny-on-call and zero publication outbox. Commit.
7. **W6 Task09** — wait for Task09 authority closure/conformance child acceptance; do not duplicate its schema,
   comparator or adapters here.
8. **W7 one-episode final E2E** — serial integration owner may change only E2E harness files plus explicitly admitted
   shared composition paths after old owners are inactive. Produce one exact causal receipt walk. Run one Supervisor
   and final-E2E adversarial review. Commit.
9. **W8 rollout** — create 3/9/32 partitions only after baseline final receipt. Verify disjointness/union=45,
   per-member terminal receipts, restart/replay, budgets and no aggregate success with unresolved member. Commit.

## Mandatory wave checks

Every leaf: `task validate`, TaskSnapshot freshness, `git diff --check`, scope/legacy/import firewall, focused tests,
PostgreSQL tests if persistent, ruff, basedpyright, and local commit SHA. W2B additionally fault-injects pre/post
dispatch timeout, response loss and Store exception propagation. W7 checks all AC1–AC8 evidence from committed sets;
W8 checks AC9. Explicitly record runner/toolchain hashes in child CheckReport. Skip, xfail, unavailable source,
unknown egress, missing receipt or missing model is fail/deny, never pass.

## Repair and stop rules

A read-only reviewer never edits product files. Repair returns to one child owner and is a separate local commit.
The same fingerprint follows repair → repair → deny/replan. Stop if a child needs a protected authority mutation,
active-owner file, unregistered legacy bridge, fourth queued FunASR request, second model instance, new provider key,
publication route/credential/outbox, post-dispatch new identity, or any 45-member partition overlap/gap.
