# VLM bounded retry review

## Outcome

PASS after one adversarial repair cycle.

The first independent Codex review found two Critical and three Warning
issues: deterministic reconcile errors could remain indeterminate forever;
the five-minute dispatch lease was shorter than the registered provider path;
backoff was caller-supplied rather than re-derived from frozen policy bytes;
SQL numeric closure needed confirmation; and pre-Responses file/client errors
could enter the wrong reconciliation state.

The implementation now:

- treats three as the total Attempt count, with `(2, 8)` persisted backoff;
- retries only explicit transient create/terminal failures;
- reconciles unknown Responses outcomes only against the original response ID;
- terminalizes deterministic retrieve errors and unclassified response failure;
- separates/quarantines unknown Files upload outcomes;
- fences provider ownership for twenty minutes and rejects longer registered
  timeout combinations;
- re-derives ordinal backoff from immutable request bytes and policy hash;
- commits one terminal Receipt with the complete ordered Attempt relation;
- preserves V1 runs as one-Attempt history and upgrades populated PostgreSQL
  data atomically.

The focused independent re-review scored 97/100 with no Critical or Warning.

## Verification

- Real PostgreSQL affected suites: 73 passed.
- Other PostgreSQL project suites: 58 passed in an earlier compatibility run.
- Focused unit/runtime/provider/migration suites: 125 passed, 2 skipped.
- Architecture boundary suite: 17 passed.
- basedpyright: 0 errors, 0 warnings.
- Ruff and `git diff --check`: passed.
- Populated `autocut` migration: six legacy Attempts and six Receipt links
  upgraded without partial state; migration ordering bug found on the first
  attempt was transactionally rolled back, fixed, and revalidated.

No Claude Code was used, per user instruction.
