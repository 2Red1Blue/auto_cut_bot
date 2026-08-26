# Local speech window wire — reviewed implementation checkpoint

The independent `review_calibration_migration` run gives bounded ALLOW for
the shared DTO/codec/projector, native endpoint, Pipeline client and their
four focused test files. No new Critical or Warning remained. The separate
Claude client-to-server test file and next-window design are still in progress
and are not covered by this decision.

## Evidence

- Root combined run: 332 passed, 4 explicitly desktop-only codec cases skipped.
  Includes local PCM fake-frame tests with NumPy/SoundFile/PyAV installed,
  exact-range oracle, wire mutation/projection, service/client, v1/shadow
  regressions and import-firewall tests. No native codec decode/model/DB ran.
- Reviewer independent run: 154 passed using fake/synthetic inputs.
- Scoped Ruff passed; three shared window modules plus Pipeline client and
  their typed pure/client tests passed BasedPyright with zero errors/warnings.
  This is not a claim that the existing dynamic native service is type-clean.
- `git diff --check` passed. The private user configuration is excluded.
- Security scanner of Pipeline media-preflight directory reported no findings.
  The generic quality scanner reports existing media complexity warnings;
  scanner file-only invocations scanned zero files and are not evidence.

## Corrections retained in the implementation

Reject nonfinite JSON including numeric overflow; retain/redecode exact raw
bytes before projection; derive actual word boundary touch; union overlapping
VAD without shortening tails; accept the SDK's optional textual key while
keeping unknown native fields closed. Repeated cancellation drains native
ownership and cannot skip queue release.

## Not complete

The new `/v2/timed-speech-window` provider path is explicit. The composed
Pipeline still uses the old full-source speech command until durable local
window ownership, physical-root split, exact replay, installed calibration and
Runtime wiring are implemented. No Stage4 Admission, real speech-model run,
publication or complete production readiness is claimed here.
