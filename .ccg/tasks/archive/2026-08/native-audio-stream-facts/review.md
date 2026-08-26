# Review checkpoints

## Mathematical presentation mapper

Root extended only the exact accepted root types in ReplayedPresentationMap.
No mapping, decoded-endpoint, rational coverage or certificate-replay rule was
changed. The old speech guard/edit admission still requires the old root; this
change grants no admission by itself.

The new physical-root mapping test first failed on the unsupported exact type,
then the focused mapping suites passed: 25 tests. Scoped Ruff and BasedPyright
(mapper plus candidate consumers) passed. Independent native reviewer:
ALLOW; independently reran 25 tests and checked unchanged speech boundaries.

## Audio layout facts

Completed by a native worker, independently reviewed by a different worker:
ALLOW. Root read the new type/decoder and production diffs, ran 75 pure DTO
tests plus 32 Source Command tests with synthetic native I/O, and 250 existing
timed Command/presentation-codec/physical-prelude/firewall regressions: 357
checks passed. Scoped Ruff and production BasedPyright pass.

The new real Source probe path requires measured sample rate/channels; old
missing leaves remain absent with identical hashes. Readback reconstruction
retains the leaf. Native layout has its own normalized metadata hash and
binds the existing exact probe-execution digest without reinterpreting the old
presentation output hash. Rehashed internally coherent foreign metadata still
fails its exact probe join. This proves value/Store-fixture consistency only.

Real FFmpeg/codec, model and PostgreSQL tests were explicitly excluded on Mac.
One existing native tamper fixture now mutates wire data because the typed
writer rejects the inconsistent value earlier; that native case was not run.
