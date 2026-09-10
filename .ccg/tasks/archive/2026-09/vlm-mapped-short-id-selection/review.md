# Clarification review

User correction: selecting existing IDs is allowed when the application has mapped
them to extremely short request-local aliases. Model-minted IDs/mappings and proof
graphs remain prohibited. Updated spec section 2.1, design section 3.1, AGENTS and
README/01/06 entry notices. The base observation schema stays ID-free; explicitly
registered selection variants can use a finite alias membership enum and null.

Selection requires program-owned one-to-one mapping, validation before dispatch,
original mapping replay, ambiguity retention and no automatic authority. Selecting
existing candidates for semantic matching does not mint candidates or physical proof.

Documentation validation: 36 local links resolved; 4 JSON blocks parsed; base response
validates unchanged; alias membership passed 3 positive and 4 negative examples.
These are schema-example checks, not runtime implementation or real-provider proof.
Scoped diff whitespace check passed. No runtime source/config/profile changed.

External analysis run 02230ced-29d7-4fbc-9d21-e1ee40f69335 returned both complete
four-section reports. Claude CLI still reports glm-5-3-flash; Codex structured model
verification is unavailable. They are advisory checks, not verified Codex+Claude
approval. The normative amendment applies the user's explicit clarification.
Final dual-model code review is not independently required for documentation-only
changes by the workspace delivery rule.

Independent read-only delta review found an ambiguity between whole-schema rejection
and unknown-alias description retention. Corrected section 3.1/6: after complete JSON
decoding validate observation body and alias membership independently; preserve a
valid body, flag invalid selection, retain raw unknown ID and never claim full-schema
success or silently replace the unknown alias with null.
