# Physical-root evidence checkpoint

Claude Code implemented only the new six-set physical-root value, strict codec
and pure tests. Root integrated the handoff and the independent reviewer
accepted the final delta (ALLOW). This is not a runtime activation or a complete
task05 acceptance.

## Corrections and verification

- A direct constructor accepted a lone-surrogate root identifier while its
  decoder refused to read it back. Root reproduced the failing test, added
  strict UTF-8 validation, and added direct/mapping/JSON negative cases plus
  Chinese/emoji positive roundtrip coverage.
- The original duplicate-key test had unrelated invalid fields. It now starts
  from a valid full payload, inserts a same-value duplicate, and demonstrates
  that a last-key-wins parser would incorrectly return the original value.
- Final root regression: **2331 passed** across the new physical-root tests,
  existing root values, existing root codec and import-firewall suite.
- Scoped Ruff passed on all three new files. BasedPyright passed on the two
  production modules (zero errors/warnings). No whole-repository type-clean
  claim is made.
- Independent reviewer: original 604 tests passed; final focused delta eight
  tests passed, with both the Warning and test-quality Info closed.
- Claude's read-only final delta review also returned ALLOW; eight focused
  tests and the final 611-test physical-root file passed in its run.
- Claude initially reported an old `tests.media` import collection issue; root
  did not reproduce it in the project uv environment and ran that suite.

All tests here are pure/synthetic. No model, database or native codec was run.
The protected private configuration was not read, changed or staged. Craft
review kept dependencies inside Kernel media and added no framework layer.

## Still required

The physical prelude Command must commit source/probe/map ownership before local
window children. Exact mapped local requests, durable results/readers, bounded
expansion/recovery, per-window admissions and shadow-local calibration remain
the next implementation batches. Existing eight-set consumers are unchanged;
this value alone grants no ownership, admission or runtime success.
