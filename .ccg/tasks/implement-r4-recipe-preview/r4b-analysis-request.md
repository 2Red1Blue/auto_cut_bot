# R4B bounded analysis request

Assess the minimal remaining R4 write path in the v213 kernel.

Target outcome: a client selects only a committed `SpanVariantSet` `variant_id`; the kernel independently rereads and validates that set; the selection creates an immutable, CAS-protected new production Recipe revision; then existing Admission, Render, and QC receive that revision as their only input.

Non-negotiable constraints:

- No arbitrary user tick, no client supplied physical span, no legacy import, and no direct Store write outside the command.
- Preserve the existing `CompileProductionRecipeCommand@1` and `BuildSpanVariantSetCommand@1` semantics.
- Idempotency must replay only byte-identical committed result; stale parent, stale variant, or CAS conflict must be rejected without mutating the previous revision.
- The implementation must be testable in PostgreSQL and on the PC WSL runtime. It must not claim a real video render unless it actually renders a committed Recipe.

Return: (1) boundary and DTO recommendation; (2) exact command/store/CAS algorithm; (3) attack/failure cases; (4) minimum tests; (5) whether Render/QC should be in this same slice or a strictly bounded handoff.
