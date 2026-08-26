# Exact terminal Command Receipt read

Native read-only investigation of models/Postgres/migrations confirms all
required columns exist. No migration is necessary for this read-only slice.
Existing read_outcome lacks expected identity/profile closure; the exact-set
reader only handles succeeded sets. Neither alone authorizes a BUSY successor.

Worker owns new `store/terminal_receipts.py`, one public method plus import in
`store/postgres.py`, and new `tests/store/test_terminal_command_receipt.py`.
Do not modify models.py, migrations, generic writers or other owner files.
Root owns metadata/review/commit. No native/DB/model execution on Mac, no
private config/legacy/Claude/spawn or commits by worker.

## API and fields

`read_terminal_command_receipt(job, *, command_slot_id, receipt_id,
expected_request_hash, expected_command_name, expected_execution_kind,
max_failure_detail_bytes) -> PersistedTerminalCommandReceipt`.
All parameters explicit, exact positive integer byte bound. Reuse existing
Store transaction/parameter-validation patterns without adding a claim/write.

Frozen result contains job, job_id, command_slot_id, receipt_id, request_hash,
command_name, execution_kind, outcome, failure_code, failure_detail_json.
Exact types/hash/UUIDs, nonempty code, failed|denied only, strict finite object
JSON. This DTO grants no committed ownership or retry decision by construction.
JSONB text is a logical JSON representation, not original HTTP bytes. Do not
advertise it as a raw-response hash preimage or silently lose numeric precision
by reserializing parsed floating values. BUSY child later reconstructs its own
closed canonical proof and checks stored raw-response length/hash.

## SQL closure

Join jobs → command_slots → command_receipts on exact ownership. Require Job
key AND profile, supplied slot/receipt IDs, expected request/name/execution kind,
receipt failed|denied, slot state equal receipt outcome, null result set pointer,
and NOT EXISTS any artifact_sets owned by that slot. A null pointer alone is
insufficient: generic initial migrations do not forbid an independently inserted
slot-owned set. Require exactly one row, no latest lookup and no FOR UPDATE.

Apply UTF-8 detail-byte cap in SQL before transferring payload text (not only
after parsing), then defensively revalidate the returned row/byte count/JSON.
No arbitrary row, wrong profile, mismatched terminal state, hidden set, missing
code, scalar detail, duplicate/nonfinite JSON or oversize data may be accepted.
Missing, running, succeeded or inconsistent rows fail closed; the reader does
not create a new slot or synthesize a failed outcome.

## Verification

Run the real public reader with fake SQL I/O, following the existing exact-set
reader test pattern. Test correct failed/denied, all identity/shape mutations,
UTF-8 bound before parse, logical JSONB formatting, SQL no-set predicate and
read-only statements. Use strict SQL expectation checks, not fixtures that
discard predicates. Scoped Ruff/types and independent review are required.
These tests do not certify PostgreSQL constraint/race behavior. Real desktop
DB acceptance and deterministic BUSY successor admission remain separate.
