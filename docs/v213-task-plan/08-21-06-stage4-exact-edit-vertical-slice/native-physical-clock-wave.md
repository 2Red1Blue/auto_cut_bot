# Native physical clock consumption checkpoint

`physical_edit/presentation_map.py` now consumes the original root, source probe,
committed v2 certificate, SourceManifest hash and audio snap calibration. Its
constructor delegates replay to the existing certificate owner. It does not
mutate root evidence, regenerate a v1 certificate, claim Store commitment or
admit an edit.

- `map_video_tick_bounds` accepts decoded frame PTS or the probe-proven source
  end and computes floor/ceil on absolute rational presentation time. Returned
  rounding bounds are not automatically valid audio sample endpoints.
- `require_av_span_covered` requires real frame/sample boundaries (a proven video
  end may be an out sentinel) and returns one common continuous segment covering
  both complete A/V spans. Two individually mappable endpoints cannot bridge a
  gap. The certificate's outward integer envelope and timing tolerance cannot
  manufacture coverage.
- Negative origins, fractional conversion, unequal stream tails and internal
  gaps are preserved. This domain does not evaluate synchronization tolerance,
  speech, visual/subtitle safety, exact choice or Recipe completeness.

Independent review: `review_calibration_migration`, read-only allow, no blocking
findings. Its additional end-sentinel/non-integer probes were then made permanent
regressions. Root verified 16 new tests plus existing exact-span/presentation
codec tests: 52 passed. Scoped Ruff and production type checks passed. No DB,
FFmpeg/model, full compiler or full Pipeline acceptance was performed.

## Next native compiler seam

Keep root-global frame/sample/visual/subtitle evidence and the replayed v2 map
unchanged. Derive speech protection directly from a closed candidate-local
Transcript/VAD window and the actual installed profile; never replace root's
Transcript/VAD and silently rehash the root. Reuse word grouping, FSMN range
merging and policy rolls from the existing pure guard owner. A word guard cannot
prove complete dialogue. Unknown/truncated/touching local evidence remains a
failure, not a request to run full-series ASR.

Construct the candidate audio domains per proven common segment, rather than
mapping four outer desired/anchor endpoints across a single interval. Every
feasible pair must satisfy complete A/V segment coverage as well as the existing
conjunctive speech/visual/subtitle/sample/frame checks. Preserve complete search
and canonical-choice identities, including actual candidate/window/plan hashes.
This work and independent physical admission are still pending; the new domain
alone does not make the existing primitive compiler production-ready.
