# Plan

1. Preserve the already-completed CUDA shadow identity/deployment work; it
   remains measurement-only and grants no normal evidence route by itself.
2. [done] Add immutable runtime measurement identity and environment-specific
   calibration capabilities. A static policy can bind independent accepted
   PC-CUDA and Mac-CPU records without replacing either historical record.
3. [done] Decouple runtime startup from dynamic capability availability. The control
   plane starts with valid static authority; timed-speech work becomes
   `awaiting_calibration` or `recompute_needed` when its frozen requirement
   cannot be satisfied.
4. [done] Add a pure requirement fingerprint and an exact reusable-evidence index.
   The fingerprint excludes Job/command ownership but includes every semantic
   input that can change the produced evidence.
5. [done] Add an append-only cross-Job whole-episode composition command. It can select
   old successful episode evidence and new recomputed episode evidence only by
   exact closure; the existing same-Job batch command remains unchanged.
6. [done] Wire dynamic runtime capability resolution into Media Preflight. It
   reads the authenticated self-measured local FunASR timing identity and does
   a fresh exact Store lookup before detector/materialization work. Missing
   capability projects to `awaiting_calibration`; a changed or malformed
   binding projects to `recompute_needed`; service unavailability remains
   recoverable `indeterminate`. Do not re-run VLM or physical evidence when
   the target requirement proves historical inputs are reusable.
7. [next] Run focused unit/PostgreSQL/integration regressions and an independent
   adversarial review. Then commit, push, and update the PC checkout before
   attempting the real PC calibration/run.
