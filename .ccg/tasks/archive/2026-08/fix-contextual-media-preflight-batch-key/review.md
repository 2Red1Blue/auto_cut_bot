# Review

Decision: pass for the batch-identity defect.

- Contextual profiles read only the committed Context Pack set.
- The same ordered Context Pack hashes are supplied to the VLM aggregate key.
- Missing or malformed contextual input fails closed.
- Legacy profiles retain their historical non-context key.
- No API fetch, VLM provider call, or Artifact mutation was added.

Verification: Ruff passed; 114 relevant tests passed and 4 PostgreSQL-dependent
tests were skipped locally. The existing V4 Media Preflight type incompatibility
remains a separate P0 and is not hidden by this fix.
