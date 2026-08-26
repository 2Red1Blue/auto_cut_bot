# Independent review — shadow-local service wiring

## Scope and authorship

- `calibration_contract`: closed Kernel measured-profile builder/decoder/tests.
- `review_calibration_migration`: service startup/fixed route/synthetic tests.
- `calibration_migration`: fixed-port client/shared dispatch/tests.
- Root: cross-layer loopback/independent-anchor tests, integration and review.
- No Claude/external model was called; these are separate native-agent runs.

Root reviewed all production diffs and reproduced the pure/client and loopback
suites. The client author independently reviewed root's integration tests;
the profile author independently reviewed the service changes. No legacy import,
installed-calibration shortcut, config edit, real model or DB was introduced.

## Verified behavior

- Native identity excludes itself; full profile identity includes it. The
  service compares actual code/model/framework/device/decoder facts, not merely
  its declared hash. The pure content builder grants no acceptance authority.
- New shadow-local, normal and full-source-shadow routes are mutually isolated.
  The shadow client takes a port and constructs one literal-loopback route;
  it shares normal single-dispatch transport/projector/error behavior.
- Independent local gold yields actual 0/7 tick error, retained raw response
  bytes and original source-clock observations. Anchors are not sent to models.
- Failed startup, invalid input, limits, queue pressure, unproved 503 and
  repeated inference cancellation do not silently become successful evidence.

## Review finding and verified repair

A pure fake-thread probe reproduced a pre-existing startup ownership race:
the first cancellation of `Service.load()` awaited a shielded model task,
but a second cancellation escaped that single wait and released the singleton
before the thread finished. New local mode shares the same startup, so root
authorized a minimal drain-loop repair and a focused regression in the same
owned service slice. This is not evidence of the cause of any prior real OOM.

The repair drains the same constructor task despite repeated cancellation.
Success still propagates the pending cancellation; constructor failure retains
the original error. Both paths release ownership only after the thread finishes.
The implementer recorded two red-before/green-after regressions. The original
reviewer independently reran both and a three-cancellation fake-thread probe;
all passed. Root reviewed the delta and reran the combined loopback suite.

Final verdict: ALLOW for this bounded measurement slice, no unresolved blocking
finding. This is not approval to enable local Runtime or publish output.

## Reproduced checks

- Root: 1,113 pure/fake-Store/client regressions, 0 skipped (the previous 923-case
  source/prelude/local-window/receipt suite plus 140 new profile and 50 client cases).
- Root: 138 synthetic startup/loopback checks, 0 skipped, using temporary NumPy
  and SoundFile dependencies and fake models/decoder; no real native video codec.
- Service author: 255 combined BUSY/window/normal/shadow HTTP checks, 0 skipped.
- All eight changed Python files pass Ruff. The three new/refactored standalone
  production modules pass scoped basedpyright (0 errors/warnings). Service-added
  regions and the new endpoint test are clean in the author's scoped check.
- Client author independently reviewed root's integration file and ran its five
  tests: ALLOW. Profile author independently reviewed service and repair: ALLOW.

The final loopback command was:

```sh
uv run --no-sync --with numpy==2.4.6 --with soundfile==0.14.0 \
  python -m pytest --import-mode=importlib \
  -o 'pythonpath=packages/autocut-kernel/src tests/pipeline' -q --tb=short \
  tests/pipeline/test_funasr_shadow_local_client_server.py \
  tests/pipeline/test_funasr_shadow_local_endpoint.py \
  tests/pipeline/test_funasr_window_client_server.py \
  tests/pipeline/test_funasr_window_endpoint.py \
  tests/pipeline/test_funasr_timed_speech.py \
  tests/pipeline/test_shadow_calibration_envelope_contract.py
```

## Limits of acceptance

Synthetic native outputs and fake decoder/Store evidence test code, not real
calibration. The normal composed Runtime remains full-source. Local authority
grammar, persisted measurements/independent acceptance, durable child/episode
admissions and desktop real execution are still outstanding. The existing
service has pre-existing type diagnostics outside added regions; no whole-service clean
type-check claim is made.
