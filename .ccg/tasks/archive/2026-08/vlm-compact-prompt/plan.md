# Compact prompt, not a silent replay change

Evidence: real episode1 response length;32768 output tokens including19560
reasoning;33,095 incomplete JSON characters,40.6% formatting whitespace.
First change only representation/prompt efficiency; no raised output budget,
hidden thinking parameter, reduced required schema fields or repaired JSON.

1. Worker owns prompt.py, request_factory.py, policy_binding.py, reuse.py,
   runtime/semantic_authority.py, runtime/composition.py and its two
   _authority/semantic-run resources, plus new test_vlm_compact_prompt.py.
   Register exact v4 compact prompt. Keep old v3 bytes/hash/default for full
   pipeline and historical replay. Builders/hash helpers resolve explicit version.
   New v4 says compact JSON/concise nonduplicate wording/minimal sufficient
   supports without omitting required facts; clarify direct visible text versus
   external OCR. Add exact proxy time_base in v4 context only.
   Semantic-only installed authority explicitly chooses v4; environment overrides
   only model/token limit are compared against that installed policy. Old frozen
   v3 execution profiles still reconstruct using v3 prompt. Reuse projection uses
   the exact registered template for each request's version.
2. Main verifies existing broader suites and real HTTP persistence/replay,
   adjusts any test that incorrectly equates semantic-only policy to legacy default.
3. Independent read-only reviewer checks old byte identity, frozen profile
   roundtrip, installed authority bindings, no hidden params, same schema/parse.
4. Commit then restart real local Pipeline; preserve failed original run. One new
   single-episode real run uses new prompt; no all-series calls. Same-key replay
   and restart must not create a second provider attempt for the same run.

Selective recompute HTTP/ledger/reader remain separate unfinished work. No SSH,
Claude, legacy code or retired ac-auto-cut-pipeline skill. Private config/media
outside Git. No worker spawns; workers are not alone and must preserve other edits.
