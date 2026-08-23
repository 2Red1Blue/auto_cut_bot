# F0 precedence and staleness map

`authority` controls the five hash-pinned v2.1.3 production-contract files.
`A`, `B`, and `C1` through `C5` are source-owner inputs only. `D` and `E`
are **context_only** availability records. `errata.execution` and
`errata.recovery` are **context_only** unavailable archive-source records:
their archive documents are not Git objects in the named authority repository,
so this intake records no invented commit, path, or hash.

The execution correction controls `none|model_invoked|business_committed|
external_intent_committed|external_effect_observed` and paired exact refs;
the incompatible execution-design phase spelling is **superseded**. The recovery
correction controls the §9.1 fingerprint tuple and zero-budget evidence
transaction; incompatible expanded-fingerprint and executable-rerun spans are
**superseded**. Any other implementation-design material is **context_only**.
No conclusion here is a Registry, generated output, implementation, or
readiness claim.
