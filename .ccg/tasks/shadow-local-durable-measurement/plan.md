# Plan

1. Inventory existing generic Claim/Receipt/ArtifactSet/Blob/CAS primitives and
   the old full-source measurement route; explicitly list what can and cannot
   be shared.
2. Freeze a sibling local command/plan/attempt/member state machine and its
   two-artifact manifest/results grammar. Keep Store ownership out of the media
   domain and authority acceptance out of this task.
3. Add Store DTOs/reader and an additive PostgreSQL migration with isolated
   local tables/constraints/triggers. Reuse transaction mechanics, not old
   protocol strings or full-source schema checks.
4. Implement command recovery and independent exact reader against the frozen
   media-domain grammar; add focused fake-store tests first.
5. Run migration/static checks, scoped tests/type/lint and independent review;
   archive only after a separate reviewer verifies no full-source or authority
   route was widened.
