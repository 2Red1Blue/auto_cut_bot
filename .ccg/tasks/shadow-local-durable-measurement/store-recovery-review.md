# Store recovery contract review

The sibling local recovery DTOs and migration `0023` were independently
reviewed twice. The first review rejected five real issues: successor slots
could not share a command slot, BUSY had no durable proof state, JSON equality
accepted type drift, successor inheritance lacked full identity closure, and
SQL null/variable handling was unsafe. The corrected change:

- keeps old full-source types unchanged;
- permits same-slot contiguous successors while preserving predecessor identity;
- persists a request-bound BUSY proof separately from a staged measurement;
- uses strict canonical comparison and complete predecessor identity checks; and
- makes SQL plan shape validation null-safe and closed.

Verification: 8 focused pure/static tests, Ruff, BasedPyright and `git diff
--check` pass. This is not PostgreSQL runtime validation; the forthcoming Store
method/command slice must exercise CAS, triggers and restart recovery on the
desktop database.

## Postgres journal tranche review

The dedicated local journal methods were independently reviewed before
acceptance. Review initially rejected six issues: source/slot replay selecting
the first attempt, a BUSY proof leaving recovery unreachable, media JSONB
round-tripping in the wrong canonical domain, non-serial member leases,
lease-expiry timestamps set before locks, and successor recovery that could
exceed its frozen budget or replay a different authorization.

The accepted tranche now verifies source ownership before creating its command
slot; uses one same-slot successor chain; persists BUSY as `not_started` while
requiring an explicit `REQUEST_NOT_STARTED` authorization; locks all prior
members before a provider lease; calculates expiry after locks; restores media
JSON using ASCII media canonicalization; and enforces both
`max_attempt_count` and exact persisted authorization identity. Independent
review found no remaining P0/P1 issue.

Verification: the focused command/store/model tests (55 tests in the local
run), Ruff, BasedPyright and `git diff --check` passed. These checks remain
fake/static only: the next work must exercise real PostgreSQL transactions,
CAS races, trigger ordering and restart recovery before this command is
activated in a pipeline.
