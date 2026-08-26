# Exact terminal Receipt reader — accepted code checkpoint

Author: calibration_contract. Independent reviewer: calibration_migration.
Root inspected the production DTO/query and tests, then reran checks.
Claude was not invoked: user explicitly requested native agents instead.

Verdict: ALLOW. No Critical or Warning findings.

- Exact Job key/profile, slot, Receipt, request, command and execution kind.
- Failed/denied only; matching slot state; null result pointer and no other
  ArtifactSet owned by that slot. One SELECT, no claims, writes or latest lookup.
- SQL UTF-8 cap before payload transfer; returned length/identity/JSON checked.
- JSONB logical text retained without binary-float reserialization. It is not
  the original HTTP proof bytes and does not itself authorize another attempt.
- No schema/migration/writer changes.

Root validation:

```sh
uv run --no-sync pytest -q tests/store/test_terminal_command_receipt.py tests/store/test_exact_committed_set_reader.py
# 203 passed
uv run --no-sync ruff check packages/autocut-kernel/src/autocut_kernel/store/terminal_receipts.py packages/autocut-kernel/src/autocut_kernel/store/postgres.py tests/store/test_terminal_command_receipt.py
# All checks passed
uv run --no-sync basedpyright packages/autocut-kernel/src/autocut_kernel/store/terminal_receipts.py packages/autocut-kernel/src/autocut_kernel/store/postgres.py
# 0 errors, 0 warnings
```

The 86 new cases use the real public reader with strict fake SQL I/O. These are
not real PostgreSQL constraint/race/JSONB execution acceptance. Desktop DB
validation and the deterministic BUSY successor resolver remain separate work.
