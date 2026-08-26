# Timed-media producer prerequisite review

Scope: PrepareTimedMediaEvidence request/command, shared calibration validator,
their tests and the HTTP media-preflight adapter. This review does not approve
the new committed reader, whole-batch finalizer, physical compiler or real run.

Implemented exact committed Source/VLM selection before effects, full request
identity binding, unconditional calibration closure and complete root binding
persistence for empty/nonempty candidates. The Runtime passes its real selector.

Independent review found a pre-existing provenance leaf check accepting boolean
or floating timing bounds because Python equality treats them like integers.
The source owner now requires positive exact integers and nonempty UTF-8 producer
ID/version strings. No normalization or semantic default was introduced.

Independent final decision: ALLOW for the six-file prerequisite slice.
Reviewer verified 131 pure tests; four PostgreSQL tests explicitly excluded.
Root combined regression: 1953 passed, four remote-only skips. Scoped Ruff and
production types passed. No local database, services or model execution.

The new required selector/root binding field has no compatibility default.
Existing incomplete artifacts cannot be promoted by filling these fields.
Whole-batch and physical-edit acceptance remain open in committed-reader-wave.md.
