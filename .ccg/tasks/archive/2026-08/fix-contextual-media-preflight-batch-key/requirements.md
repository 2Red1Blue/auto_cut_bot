# Requirements

1. Contextual VLM and Media Preflight must derive the same aggregate batch identity.
2. Media Preflight may use only an already committed immutable Context Pack set.
3. Missing contextual packs must fail closed; legacy non-context profiles remain replayable.
4. Recomputing the lookup must not refetch the external metadata API or invoke VLM.
5. Focused tests must cover contextual, missing-pack, and legacy behavior.
