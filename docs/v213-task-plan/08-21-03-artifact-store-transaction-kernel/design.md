# Artifact Store Transaction Kernel Design

> **Supersession (2026-08-23):** the product-first design in
> `research/product-first-minimal-persistence-design.md` is the authoritative
> implementation design for this task. In particular, `execution.command_slots`
> and `execution.command_receipts` are part of this first slice; recovery,
> outbox, admission evaluation and blob lifecycle are not. The sections below
> remain only as historical rationale where they do not conflict.

## Boundary

This task implements the new `autocut_kernel` authority Store only. It does
not read old ORM models, does not migrate legacy rows, and does not expose a
Runtime table-write API. The physical authority is
`10-postgresql-physical-schema-and-data-lifecycle.md`, bound before coding to
the current immutable authority baseline and frozen source hashes.  Entry is
denied unless the authority worktree is at
`baf667f797ac7d4eb34e48caad8047fb07433c9c`, the total-contract raw SHA-256
is `c34af7451919ad9a895644b40136062834b7ba9e857139f10b61f7dc51be67e9`,
and the physical-schema raw SHA-256 is
`6adbb95e531184d5229351d0cfec6250b415db0b9d4ef26be077a87cda0cd674`.
This admits only the current v2.1.3 baseline: an independently accepted F1
or later explicitly admitted authority amendment supersedes it and requires
fresh task/context/predecessor admission before any implementation change.

The first vertical slice deliberately owns only the irreversible core:

```text
storage.blob_objects (referential prerequisite only)
  + ArtifactSet (commit marker)
  + immutable Artifact revisions
  + ordered Set members
  + exact LogicalHead CAS/fencing
```

`artifacts.canonical_object_id` is a required FK. Therefore the migration also
creates `storage.blob_objects` with its authoritative identity fields and test
fixtures seed verified objects. Blob claim/retain/GC behavior is explicitly
deferred; the first slice does not expose a partial Blob service.

It uses PostgreSQL in integration tests, not SQLite, because deferred
constraints, composite foreign keys, row locks and transaction isolation are
the behavior under test. Blob retention, dependency extraction, command slots,
recovery, outbox and projections remain later packs; this slice exposes no
fake versions of them.

## Commit algorithm

`commit_artifact_set(request)` is the only write API. It receives already
canonical Envelope bytes, verified content hashes, chain identities, expected
head refs and complete set-member metadata.

1. Reject duplicate set/member/chain identities before SQL.
2. Sort all affected canonical chain keys by JCS scope bytes and lock existing
   `logical_heads` rows in that order.
3. Create the committed set marker, immutable artifacts and members in one
   transaction.
4. For every chain, require either a null expected head for revision 1 or an
   exact current `(artifact_id, content_hash, revision, fencing_token,
   write_state)` match; insert/update head with revision/token increment.
5. Commit only after the deferred database constraints prove exact member count
   and same-chain parent/head composite references. Any mismatch rolls back the
   entire set.

The API returns the committed exact refs. A commit-response-lost caller must
query by its immutable set hash/idempotency layer in the later Command pack;
it may not replay a changed set.

## Database constraints that must carry the proof

- Artifact revision uniqueness includes namespace, scope kind and scope hash;
  `logical_id` is not global.
- Parent and head composite FKs include chain identity, logical alias, exact
  artifact ID/hash and revision so cross-chain attachment is impossible.
- `logical_heads` has one row per canonical chain and unique
  `(namespace, logical_id)`.
- Artifact sets are committed-only and members are non-empty, ordered and
  reference exact artifact hashes.
- Runtime database roles have no INSERT/UPDATE/DELETE grants on authority
  tables. The kernel Store principal is the only writer.

## Test-state policy

The vertical slice runs only against ephemeral test PostgreSQL. It may expose
constraint failures and evolve request types, but it has no Platform adapter,
no publishing capability and no external side effect. A test failure rolls
back; it never creates a partial visible ArtifactSet.
