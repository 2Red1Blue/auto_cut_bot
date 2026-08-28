# Design review — WindowContextPack

## Result

**Go for the pure P0 contracts and deterministic selector.** Do not implement
the external HTTP adapter or alter prompt v6/parser v4 until the concrete API
endpoint, authentication contract and raw response schema are frozen.

## Verified boundaries

- `SourceKnowledgeInputSet` is owner input, not a prompt serialization format.
- Every model-visible external fact originates in one immutable Pack; historical
  replay reads that Pack rather than fetching dynamic API data.
- Missing/ambiguous bindings and API failure become an explicit `video_only`
  Pack, never a mixture of stale and fresh assets.
- Anti-spoiler visibility uses `external_episode_ordinal` from an explicit
  binding, not local file ordering.
- API subtitles, shots and highlights stay outside VLM input and Stage 4.
- Rich context-assisted interpretations require a new prompt/parser version;
  existing V6/V4 artifacts cannot be reinterpreted.

## Adversarial findings corrected during design

1. A local episode index could differ from the API's season/chapter ordering.
   The binding now requires `external_episode_ordinal`; visibility predicates
   only use that value.
2. A per-field source marker would inflate output and let API names masquerade
   as visual labels. The schema groups video observations separately from
   context-assisted interpretations.
3. A tokenizer exactness claim would be provider-dependent. The policy fixes a
   deterministic estimator plus an 8 KiB hard byte limit and records dropped
   groups.

## Entry gate for the future HTTP adapter

The owner must supply the endpoint contract, authentication method, series and
episode response schemas, and an explicit local-to-external episode mapping.
Until then the implementation stops after fixture-backed normalization and
selection; it must not infer an API shape from `autocut-core` legacy code.
