# Design

```text
admitted Stage 4 Recipe
  → RenderCommand claim
  → deterministic FFmpeg render into immutable CAS
  → structural/media/editorial/local-release QC
  → atomic succeeded Receipt + frozen ArtifactSet
  → provenance-bound OutputReader
  → authenticated outputs/content API
  → same-origin Next BFF
  → read-only highlight list + video player
```

## Authority and atomic visibility

The successful Receipt and exact ArtifactSet are the only visibility root.
`current.json` is a recoverable convenience pointer and cannot authorize an
output. The Store reader joins the succeeded command slot, Receipt,
ArtifactSet and exact member identities, then validates closed payloads and
asset hashes before returning an output DTO. Orphan CAS files, partial sets and
QC-denied sets are invisible.

The ArtifactSet contains at least the admitted Recipe reference/hash, render
manifest, content-addressed MP4 asset, all QC reports, local-release decision
and producer/policy identities. Receipt commit and ArtifactSet visibility occur
in one PostgreSQL transaction. Files are staged and hashed before commit; a
crash before commit leaves an invisible orphan eligible for later GC.

## HTTP surface

- `GET /v1/pipeline/outputs?limit=&cursor=` returns stable, opaque pagination
  over validated committed outputs.
- `GET /v1/pipeline/outputs/{artifact_id}/content` repeats provenance checks,
  resolves only the stored safe locator, rejects symlink/path escape, verifies
  length/hash according to policy, and supports byte Range with 206/416, ETag,
  Accept-Ranges and `X-Content-Type-Options: nosniff`.

The JSON DTO contains run/profile/Receipt/ArtifactSet/artifact identities,
hashes, media type, byte length, duration/QC summary, created time and a relative
playback URL. It contains no filesystem path.

## Frontend boundary

Next uses a catch-all same-origin server route to call the Pipeline API and add
the service credential. Browser components use the real snake_case run/resume/
status/output DTOs and Idempotency-Key. The existing fake `/jobs`, cancel and
media CRUD calls are removed. The media page is read-only because publication
and deletion are separate commands with their own authority.
