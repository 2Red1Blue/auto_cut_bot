# Shadow-local measured service wiring

Execute the accepted [service plan](../../../../../docs/v213-task-plan/08-21-05-media-preflight-calibration/shadow-local-service-implementation-plan.md).

1. Agent calibration_contract owns the new Kernel shadow-local service-profile
   content builder/strict codec and its pure tests. Derive both non-circular
   identities; do not manufacture accepted Records or deployment permission.
2. Agent review_calibration_migration owns `deploy/funasr/service.py` and new
   synthetic local-shadow startup/endpoint tests. Reuse exact window execution;
   strictly separate normal, old-shadow and new-local-shadow modes.
3. Agent calibration_migration owns the existing window HTTP client refactor,
   fixed shadow-local client wrapper and its tests. Share wire/error behavior;
   preserve normal URL restrictions and single dispatch.
4. Root owns cross-layer integration tests and documentation. Independently
   review every delivered diff, verify startup hashes against the pure builder,
   run synthetic loopback client/service/anchor projection and regression suites.
5. Fix evidenced findings, archive this bounded slice, commit/push only its files.

Review-driven repair within the existing service owner: repeated cancellation
of `Service.load()` could release the host singleton while its `AutoModel`
thread was still running. This predates the new mode, but the new route inherits
the same startup. Drain that one worker despite repeated cancellation before
releasing ownership; add fake-thread cancellation/failure regressions. Do not
expand this into a service lifecycle rewrite.

Constraints: no Claude or new agent creation; no config edits; no real models,
DB or native video codecs on Mac; no legacy imports or altered normal profile
authority; no arbitrary caller-selected endpoint/mode. Fake model/decoder and
ephemeral HTTP tests are allowed. Root retains ownership of integration files;
agents must not revert each other's edits. Full-source runtime remains active
until separate local persistence, calibration acceptance and installation close.

Acceptance: strict profile grammar and identity drift rejection; actual decoder
measurement before readiness; fixed route/mode separation; original bytes and
independent anchor projection; limits, auth, BUSY/no-dispatch and cancellation;
normal/old-shadow regressions unchanged. Test results are not real calibration.
