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
