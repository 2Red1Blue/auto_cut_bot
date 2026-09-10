# User clarification: selecting pre-mapped short IDs is allowed

Amend documentation only. The user clarified that some tasks legitimately require choosing IDs, but these MUST be extremely short IDs pre-mapped by the application. The previous blanket ban on selecting IDs was too broad.

Keep observation richness and the ban on model-invented IDs/proof graphs. Allow optional selection of existing entities/objects through request-local aliases such as a/b/c with frozen program-side mapping and membership validation. No aliases minted in the same response, no long durable IDs, no model-defined mapping, no automatic proof/authority from a selection. An enum of pre-supplied aliases is transport membership, distinct from arbitrary business classification enums. Basic observation output need not contain IDs; task-specific selection variant may. Unknown/ambiguous selections stay unresolved without discarding description or re-running VLM for downstream closure. Replaying must use original mapping, not current reordered lists.

Analyze edge cases and contradictory wording to fix in AGENTS, backend spec, design14 and entry notices. Return concise Options/Recommendation/Risks/Validation. No source edits. This is a user-authored normative clarification; no runtime approval is sought.
