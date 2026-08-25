# Implementation

1. Close Render/QC/Release artifacts and the output-list DTO; add tamper tests.
2. Implement `RenderCommand` around the existing deterministic render/QC code,
   committing one exact ArtifactSet and terminal Receipt transactionally.
3. Implement Store methods and a provenance-bound `LocalOutputReader`; cover
   wrong Receipt/set/member, hash mismatch, restart and replay.
4. Register the render stage after Stage 4 in the real HTTP runtime; project
   exact Receipt identities and preserve CAS/idempotency behavior.
5. Add authenticated paginated outputs and Range content endpoints with safe
   locator resolution, ETag and no absolute-path disclosure.
6. Add a same-origin Next BFF, align run/resume/status DTOs and remove phantom
   gateway/viz rewrites.
7. Convert the media view to a read-only committed-highlight list and `<video>`
   player; add loading, empty, denied and playback-error states.
8. Run unit/integration security tests, a real FFmpeg single-output test and a
   browser test that submits a run, observes status, lists and seeks the MP4.
9. Independently review fail-closed visibility and replay before enabling the
   task in the 45-episode real run.
