# Render QC Release

## Goal

Consume only an admitted Stage 4 Recipe, deterministically render it, run
structure/media/editorial/local-release QC, atomically commit the successful
Render/QC/Release ArtifactSet and make the resulting local highlight readable
and seekable from the current web frontend. This task performs no external
platform publication.

## Requirements

- Rendering is a Kernel Command with Command/Attempt/Receipt/ArtifactSet
  provenance. Runtime and UI cannot invoke FFmpeg or promote files directly.
- A visible output must be reachable from one succeeded Receipt that references
  one exact frozen ArtifactSet containing Recipe provenance, render asset hash,
  QC results and local-release decision. Directory scans and `current.json`
  alone are never authoritative.
- QC is fail closed across structural completeness, decodability/duration,
  black/frozen frames, loudness/A-V sync, edit continuity, subtitle bounds and
  local output policy. A denied/failed/indeterminate result is not listed.
- CAS bytes are immutable. The database commit is authoritative; local pointer
  promotion is derived and crash-recoverable. Commit-ack loss and replay cannot
  create a second visible output.
- Add a provenance-bound paginated output reader and authenticated API. Content
  reads revalidate the succeeded Receipt/ArtifactSet/member and asset hash,
  support HTTP Range/206 and ETag, and never accept or reveal an absolute path.
- The Next frontend calls the real Pipeline API through a same-origin server
  BFF that injects the API key server-side. Browser code never receives service
  credentials. Remove phantom jobs/media CRUD/viz-server assumptions.
- The media page is a read-only list of committed highlights with title,
  source/run provenance, duration/QC state and an HTML video player supporting
  seek. Legacy UI interaction patterns may be reimplemented; its data path is
  not reusable authority.

## Acceptance Criteria

- [ ] One real HTTP run reaches the render stage, commits a succeeded
  Render/QC/Release ArtifactSet in PostgreSQL and produces a local MP4 whose
  bytes match the committed asset SHA-256.
- [ ] QC denial, wrong Receipt/set membership, missing member, hash tamper,
  path traversal, symlink escape and orphan CAS/current files are not visible.
- [ ] Replay and process restart return the same Receipt, ArtifactSet and output
  item without rerendering or duplicate visibility.
- [ ] `GET /v1/pipeline/outputs` returns only provenance-valid items; content
  returns authenticated 200/206 with ETag, Accept-Ranges and `nosniff`.
- [ ] From `/pipeline`, a user can submit and observe the real run; from
  `/media`, the resulting highlight remains listed after restart and can be
  played and seeked. No absolute local path or API key appears in JSON/DOM.
- [ ] External publication remains disabled; this task cannot mint
  `publish_decision=allow` for any platform.

## Notes

The existing local renderer/QC/promotion code and its real FFmpeg tests are
algorithm candidates. They must be wrapped by the current Kernel/Store
authority rather than exposed by scanning their output directories.
