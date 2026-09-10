# Shadow-bootstrap collection review — 2026-09-10

## Reviewed change

`2cce5cb7` introduced a dedicated bootstrap HTTP port and durable observation
command. Independent review found two Critical issues before acceptance:

1. the request accepted non-shadow source/destination Jobs;
2. an unknown HTTP dispatch was converted to a terminal Receipt, preventing
safe recovery.

## Fixes applied

`b49a25e2` resolves both findings.

- `CollectShadowBootstrapObservationRequest` now requires both Jobs to use the
  `shadow` profile before claim/materialization.
- `ShadowBootstrapObservationDispatchUnknownError` keeps the claim running and
  writes no terminal Receipt if a timeout, truncated response, or transport
  loss makes provider completion unknowable.
- artifacts use the isolated
  `autocut_observation/shadow_bootstrap/<request-hash>` scope, not
  `pipeline/job`.

The remaining runtime-registration finding is intentionally open: collection
is callable through the new Command/port but is not yet an automatic normal
Pipeline stage. Bootstrap must stay outside normal MediaPreflight authority.

## Verification

- focused unit tests: 32 passed;
- Ruff and diff whitespace checks: passed;
- real desktop GPU invocation through the project HTTP port on `r0-ep01`:
  272 ASR observations, 27 VAD observations, one recorded one-tick rounding
  repair, `authority_eligible=false`.

The raw request/response and projection summaries are held only on the desktop
under `/home/laiu/r0-artifacts/shadow-bootstrap/r0-ep01/`.
