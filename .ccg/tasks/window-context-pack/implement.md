# Implementation order

1. Add pure `autocut_kernel.context_pack` values, normalizer and selector.
2. Add the pipeline HTTP adapter and durable `context_prepare` Command. It
   captures the two configured resources once, writes raw bytes as an immutable
   Blob, and commits a `window_context_pack_set` ArtifactSet before VLM starts.
3. Register prompt v7 as a context-aware input variant while keeping the V4
   response schema and parser intact.
4. Bind a pack to request payload/identity/hash and verify it cannot be used
   with a v6 policy.
5. Update the semantic-only command order to `source_prep -> context_prepare
   -> vlm`, then ensure replay VLM reads the committed PackSet rather than
   refetching. Test all deterministic paths. A live run is only attempted after the
   endpoint, credential and explicit local-to-external episode binding are
   available; the run must use the new v7 policy and retain its debug output.
