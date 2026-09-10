# Review and verification

Scope: VLM observation-only design and mandatory repository specification. No runtime,
provider profile, database, historical response, or paid call changed in this task.

## Findings and corrections

- Existing V23 wire and the previous rich redesign both kept IDs/index references,
  business enums and proof/candidate closure in the VLM's responsibility. The new
  normative source forbids these, including disguised forms and prose protocols.
- Root AGENTS and backend index require reading the normative specification. Contract
  README, 01 and 06 explicitly label conflicting prior VLM clauses as superseded.
- Independent read-only review found one wording loophole: the example Prompt said
  identifiers were unnecessary instead of prohibited. Fixed to prohibit machine IDs,
  refs and proof structures explicitly while permitting identifiers actually visible
  in footage. Added treatment of disguised machine protocols inside prose.
- Stage1-3 new schemas/readers and ambiguity propagation are explicitly unfinished
  implementation contracts, not claims of automatic compatibility with V4.
- Independent follow-up reviewed the three changed passages and confirmed the wording
  loophole closed with no new finding in that bounded recheck.

## Checks executed

Using the existing repository .venv and jsonschema Draft202012Validator:

- 37 local Markdown links resolve across the seven changed/new design/spec documents.
- Both new JSON blocks parse; the schema is valid and the example validates.
- Three additional positive examples validate (empty supplemental moments, null time,
  unparseable time prose); nine negative examples reject disallowed machine fields
  or nontext structured/numeric time hints.
- No enum or const in the target schema. Prompt and boundary inspection checks address
  disguised indices and free-text machine protocol; JSON Schema alone does not.
- git diff --check passed. Initial system Python lacked jsonschema; using the existing
  project interpreter resolved the validation dependency without installing anything.

These validate documentation examples only. Runtime automated gates, media fidelity,
PostgreSQL replay and real whole-series execution remain unverified/unimplemented.

## External analysis accounting

Supervisor run: 9477dfa7-4a66-460c-b700-2528a75e72e4. Both report files completed with
Options/Recommendation/Risks/Validation. Codex launcher requested gpt-5.6-luna; its
structured model verification was unavailable. The Claude CLI route reported actual
model glm-5-3-flash, so this is NOT verified Codex+Claude approval. Reports were used
only as non-authoritative option/risk checks; proposals to call narrative authoritative,
auto-compile old V4 shapes, or assume no audio purely from missing ASR were rejected.

The normative responsibility boundary is the user's explicit design decision, applied
to documentation here. No runtime implementation approval is claimed. Documentation-only
changes do not independently require dual-model final code review under the workspace
delivery rule. The existing configured model routing was not modified.
