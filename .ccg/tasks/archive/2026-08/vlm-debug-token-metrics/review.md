# Review — 2026-08-28

Real VLM terminal evidence exposed that substring-based `token` redaction hid
max_output_tokens and every usage counter. Known nonnegative exact integer
metrics are now visible, with credentials still redacted.

Independent review found an initial widening: generic recursion within token
details could expose an unknown string child. Fixed by a closed count-key and
integer-value allowlist; unknown children and nested objects are fully redacted.
Independent re-review confirmed closure.11 tests, Ruff and BasedPyright passed.

Read-only retrieval of the original provider response (no new generation)
recovered usage:34175 input,32768 output,19560 reasoning,66943 total tokens.
Original debug was preserved; recovered diagnostics are outside Git. The
provider's `length`/incomplete terminal and failed Receipt were not modified.
