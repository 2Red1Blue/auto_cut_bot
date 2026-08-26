# Candidate-local PCM extraction review — accepted slice

Scope: source-clock exact sample tracker, native extraction/private WAV input,
shared package installation, synthetic tests and desktop-only codec tests.
This is not a new HTTP endpoint, local-window Command, accepted calibration,
physical-root migration or complete Pipeline production activation.

Independent reviewer: `review_calibration_migration`, read-only separate agent.

## Findings fixed before checkpoint

1. **Critical — cancellation released model ownership too early.** Repeated
   cancellation could escape a drain handler; the first cancellation also
   discarded the inference timeout. Both full-source and local paths now use
   one typed serialized helper, preserving the original absolute deadline and
   draining repeated cancellation under the lock. A deadline invokes the fatal
   process exit before native ownership can be released. Fake-worker tests
   cover repeated cancellation and cancellation followed by the deadline in
   both paths.
2. **Warning — decoder secondary I/O escaped source hashing.** The verified
   top-level descriptor did not itself prohibit codec/container sidecar or
   network opens. The native decoder now supplies a deny-all `io_open`
   callback. Synthetic tests assert that callback is actually wired and rejects
   external reads while cleaning only output created by this invocation.
3. **Determinism defect found in implementation tests.** libsndfile FLOAT WAV
   output includes a PEAK wall-clock timestamp. A bounded parser validates the
   known RIFF header and zeroes only that metadata timestamp; it stops at the
   data chunk and never rewrites PCM. Repeated output hashes and decoded WAV
   sample tests cover the result.

## Evidence and limitations

- Pure tracker: 12 tests. Independent sample oracle: 6 parameter cases,
  exercising 1,260 windows. Negative/nonzero PTS, mixed time bases, exact frame
  slices, gaps, overlap, rate/channel drift and byte/work limits covered.
- Native synthetic and existing protocol suite: 73 passed under a temporary
  NumPy/SoundFile/PyAV dependency overlay. Native decoding/model calls are
  replaced; actual WAV samples are inspected by the model callbacks.
- Existing Prepare/candidate-guard/exact-span regression group: 144 passed.
- Architecture group: 18 passed. Scoped Ruff, diff check, pure/new-test type
  checking passed. Full `deploy/funasr/service.py` type checking is not green:
  its existing dynamic-model diagnostics and absent third-party stubs remain.
- Reviewer independently ran 66 tests and deliberately skipped four desktop
  real-codec cases; final verdict ALLOW for this bounded implementation scope.
- The combined Mac regression command completed with **253 passed / 4
  intentionally skipped**; it did not run real codecs/models.
- Desktop Ubuntu-24.04 WSL at commit `fcc4b7ae`: **4 real AAC/MP4 codec cases
  passed in 2.23s**, CPython 3.13.13, PyAV 18.1.0, NumPy 2.4.6, SoundFile
  0.14.0. Both 44.1 kHz and 48 kHz, zero and nonzero PTS origins, exact WAV
  sample comparison, both model input callbacks and private-file cleanup ran.
  Model callbacks were deliberately fake; this is real codec acceptance, not
  real SenseVoice/FSMN inference, Profile activation or DB acceptance.
- The standalone desktop test uses `--noconftest`: the repository-wide root
  conftest requires Agent/session packages unrelated to this self-contained
  codec suite. The codec test owns its temp directory and fake model modules;
  no Agent config, service or database is accessed. Initial launch without
  this isolation failed on missing `certifi`, before tests ran.
- Desktop setup bypassed its stale inherited proxy for this command only and
  bootstrapped a newer uv in its tool cache (existing uv could not select
  Python 3.13.13). No global proxy, service config or existing virtualenv was
  changed. Source transfer/prefix decode costs remain; per-frame limits are
  not a guarantee about total native process RSS.
