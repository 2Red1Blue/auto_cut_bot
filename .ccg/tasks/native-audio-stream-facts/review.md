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

Implementation in progress in disjoint source preparation/manifest files.
Not accepted yet. No native model, codec or database run is claimed.
