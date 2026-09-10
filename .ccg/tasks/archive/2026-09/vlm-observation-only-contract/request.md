# User-directed VLM observation-only redesign

Design/documentation task only. User explicitly rejects VLM-generated IDs (including local aliases), enums, editorial candidates and evidence/proof structures. VLM should richly describe what it sees and its own interpretations. Output richness is encouraged; cross references and proof obligations are not. Define input/output, field ownership, optional rough time hints, screen text versus audio capabilities, downstream compilation with unresolved ambiguity, and mandatory repository rules preventing recurrence. Preserve historical runtime semantics. Do not implement or deploy a new runtime in this task.

Current 01-vlm.md says V23 requires local IDs, fixed fact/event enums, exactly matching support and candidate measurement closure. Existing 06 design forbids mechanical output but retains rich graph/candidate expectations. Merely switching to core-only with fact/event IDs would violate this new requirement.

Assess minimal prose-first versus shallow JSON wire options, mandatory prohibitions and migration acceptance. A fixed JSON key is acceptable as a container, not a semantic enum the model must classify into. Interpretation must remain model opinion, not truth just because software attaches IDs/provenance. Missing or invalid coarse time must not discard useful narrative. No full-series new paid run to validate documentation.

Return Options, Recommendation, Risks, Validation. Analyze only; no edits. Explicitly flag any inference not established by supplied code/docs. Requirements originate in the user's correction and are not contingent on model approval.
