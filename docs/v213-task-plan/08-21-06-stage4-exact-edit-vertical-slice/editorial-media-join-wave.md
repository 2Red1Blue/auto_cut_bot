# Stage3 to committed media: physical-input join

## Scope

After the exact timed-media batch reader, connect admitted editorial Blueprints
and their real Catalog to that batch. This is a read-only shared Kernel seam,
not a new optimizer, evidence producer, or physical admission decision. It does
not invoke providers, claim Commands, create Recipes, render, or publish.

## Owner and API

Add `pipeline/editorial_timed_media_inputs.py` and focused tests. Reuse existing
Stage3, Stage2/predecessor and whole-batch readers instead of parsing their
payloads again. The entry accepts the full typed Stage3 request/outcome, full
typed timed-media batch request/outcome, installed resolver and explicit limits.
Return admitted semantic values plus compact candidate/alternative bindings;
never retain every episode's root/transcript/window DTOs.

1. Reread Stage3's exact 3N+1 and independently recompute its admission using
   `read_committed_editorial_blueprints`. Read its exact Stage2/Stage1/semantic
   predecessors with `read_committed_editorial_blueprint_inputs`.
2. Require the same Kernel Job UUID, Job/scope, exact Source receipt/set/member,
   complete semantic selector and actual committed VLM aggregate. A matching
   candidate ID, source hash, or transport outcome equality alone is insufficient.
3. Reread the complete timed-media batch with its existing bounded reader,
   including episodes unused by a Story. Require current `render_source` and
   `semantic_analysis` Source authorization through the shared owner checks.
4. Traverse every Story/Beat/requirement/alternative in frozen order. Resolve
   each `candidate_catalog/candidate` reference against the exact Catalog member.
   Preserve source constraints, physical requirements, duration, SpanPolicy,
   preference order and multi-candidate alternative grouping. Semantic material
   assignment remains a feasibility witness, not a final selection.
5. Resolve the Catalog Candidate's `vlm_semantic_pack/vlm_candidate` owner into
   the actual semantic pack and raw VLM hypothesis. Match source/window/hash,
   frame-index and source-clock/time-base identities to the child request.
   Candidate evidence ordinal is the raw pack's order, not Catalog sort order.
   The timed window's `vlm_candidate_sha256` uses the raw hypothesis's media
   canonical hash, not the derived candidate ID or Catalog hash.
6. Retain only compact child index, candidate ordinal/raw hash and exact five
   member references alongside the editorial requirement. Physical compilation
   later rereads the selected episode using the existing single-child reader.
   Direct DTO construction confers no persistence or admission authority.

## Tests and non-goals

Use real existing Stage1/2/3 Command fixtures with raw generation/attempt audit,
plus real Prepare/finalizer fixtures sharing one Source/VLM predecessor. Do not
mock the exact readers or return a caller-authored admitted Blueprint. Cover a
valid joined chain, foreign Job/set/owner, missing or reordered evidence, wrong
candidate ordinal/raw hash, dropped alternative and revoked render purpose.
Assert no provider calls, writes or detector reruns on the read path.

The next compiler change must consume candidate-local closed timed evidence and
root-global frame/sample/visual/subtitle evidence with the committed v2 piecewise
clock certificate. Current `physical_edit/exact_span.py` assumes root-wide
Transcript/VAD coverage and a primitive single presentation interval. Replacing
root Transcript fields with local data would change its hash and invalidate the
certificate; flattening internal gaps would invent coverage. Neither is an
acceptable adapter. This join does not claim those compiler changes are complete.

Real PostgreSQL, native model and end-to-end acceptance remain on the desktop.
No legacy imports, hidden defaults, full-root ASR requirement, fixture Recipe,
external publication, or new governance framework is introduced.
