# WindowContextPack implementation plan

This task introduces the sole legal path from an external narrative API to a
VLM request:

```text
HTTP response -> immutable snapshot Blob -> strict normalizer -> explicit owner map
              -> source-hash verified binding set -> deterministic WindowContextPack -> prompt v7
```

The implementation is deliberately split into a pure shared kernel package and
a pipeline adapter.  The shared package receives already-fetched JSON and has
no HTTP, environment, database, legacy, ASR, VAD, subtitle, shot, or highlight
dependency.  The adapter may fetch the two configured API resources and write
debug artefacts, but cannot render a prompt itself.

## Acceptance criteria

1. A raw response is represented by an immutable, hash-bound snapshot that
   contains no credentials.
2. The normalizer only projects stable series data, episode title/summary,
   character cards, and relationships with an explicit `known_from` ordinal.
   It never projects subtitles, shot/highlight timing, or an unclassified
   full-series synopsis.
3. An owner supplies only `(local relative path, local episode index, external
   episode/chapter id, ordinal)`; Context Prepare combines it with the actual
   committed SourcePrep hash into the binding set.  No filename,
   upload order, title matching, or API list order inference exists.
4. The selector is deterministic and produces either an API-assisted pack or
   a hashable `video_only` pack with a reason code.
5. A prompt v7 can only be built from a `WindowContextPack`; v6 requests are
   unchanged.  Context is labelled as non-video evidence and the V4 response
   schema/parser remains unchanged for this compatibility increment.
6. Unit tests cover spoiler removal, binding duplicates/conflicts, graceful
   video-only degradation, deterministic selection, immutable Blob capture,
   V7 request binding, and legacy request-hash preservation.

The real HTTP adapter is configured only through the private
`AUTO_CUT_BOT_PIPELINE_METADATA_API_BASE_URL`,
`AUTO_CUT_BOT_PIPELINE_METADATA_API_KEY`, and
`AUTO_CUT_BOT_PIPELINE_CONTEXT_OWNER_MAPS_JSON`; no endpoint, auth convention,
or episode mapping is inferred at runtime.
