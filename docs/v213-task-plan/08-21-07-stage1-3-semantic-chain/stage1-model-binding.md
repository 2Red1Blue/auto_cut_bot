# Stage 1 production model binding — 2026-08-26

## Status and scope

Implementation decision for task 07, following its owner corrections 6–8.
The unimplemented Stage 1–3 production owner must use these rules; the old
prototype is not a compatibility path. The value/graph-algorithm modules below
do not enable a Runtime stage or claim Stage 1 acceptance. Update the Stage
1–3 wire schemas and affected normative examples together with the real compiler
before activation; do not treat old partial contribution assets as executable.

## 1. Immutable member identity, not a fabricated database ID

The existing Store allocates artifact UUIDs during commit and its exact reader
returns Receipt/ArtifactSet/member ordinal plus chain identity. A pure compiler
cannot honestly construct an ArtifactRef from a not-yet-created UUID.

The semantic payload reference owner is `semantic_chain/member_refs.py`:

- `SemanticMemberIdentity`: exactly artifact_type, logical_id, revision,
  scope={namespace,kind,key}, content_hash.
- `SemanticObjectRef`: exactly member_ref, object_type, object_id.
- Both are immutable values, strict at construction and mapping boundaries.
  Unknown fields, bool/float revisions, invalid Unicode/hash and wrong types fail.
  Equality and ordering never use object_id alone.
- Extracting an identity from ArtifactMember verifies its payload content hash.
  Extracting it from CommittedArtifactMemberReference is only a value projection,
  not proof of database admission.
- References are resolved only against the Command's exact Store-read input
  closure or its own pending output set. A future exact output reader must resolve
  same-set identities against all eight members from one succeeded Receipt/Set.
  Missing, ambiguous or wrong-scope/type/revision/hash identities reject.
- Receipt/Set/ordinal remain commit provenance in the Command input/audit and
  committed reader. They are not replaced by chain identity. Never search a
  current head or another set to make an unresolved reference succeed.
- No database IDs are inserted into immutable payloads after commit. No
  synthetic artifact ID, dual reference representation, legacy alias or
  compatibility coercion is permitted for the new semantic owner.
- The historical generic ArtifactRef primitive is not silently reinterpreted.
  Stage 2/3 and Stage 4 semantic consumers must migrate to this same owner
  before the Stage 1–3 production chain is activated.

## 2. Acyclic member construction

Payload hash dependencies must follow this order (references may skip earlier
members, never point to a later member):

```text
committed Source/Window/VLM input closure
  → EventCardSet
  → EpisodeDigestSet
  → NarrativeGraph
  → EvidenceDiagnostics / ConflictDiagnostics
  → CoverageLedger
  → DependencyClosureProof
  → CoverageAdmission
```

EventCards use raw VLM evidence, not Graph Fact references. Graph Event nodes
point to the corresponding EventCard; facts/entities within Graph use local IDs.
Digests can cite EventCards and VLM windows, not Graph or Ledger.
Conflict claims are local objects owned by ConflictDiagnostics.

CoverageLedger owns seeds and coverage-window objects. A row refers to its seed
by local seed ID. A seed root targeting its own coverage window is a closed local
selector, not a member reference containing the Ledger's own content hash.
After the Ledger is hashed, the proof builder expands local selectors into full
SemanticObjectRefs. Neither Ledger seeds nor diagnostics may point to the
not-yet-built closure proof or Admission. Seed isolation is determined in the
proof; it must not be backfilled into an already-hashed Ledger.

Admission's subject hash is computed from the canonical identities of exactly
seven business members. It excludes Admission and is not embedded back into any
of those seven members. It is distinct from the Store's full eight-member set hash.

## 3. Lossless VLM observation projection

The first production Graph needs an `entity` node with exactly
`attribute_type=entity, entity_kind, display_label, visual_description`.
entity_kind preserves person/object/location/screen_text_source from the exact
VLM observation. All four have evidence; person alone is not proof of an
accepted cross-window character identity. Name/similarity never silently merges.

Every VLM Fact is retained, including standalone facts not cited by an Event:

- label = original summary; predicate = original fact_kind;
- subject_node_id resolves the exact VLM subject's Graph entity;
- a non-null object_ref becomes an entity_ref value, otherwise value is the
  original summary as text;
- source evidence retains the exact raw observation identity.
- Both subject→fact and entity_ref-value→fact are registered dependency arcs.

EventCard fields remain factual. Its range is the mapped coarse VLM evidence,
never a physical edit endpoint. Raw VLM evidence uses distinct object types
`vlm_entity|vlm_fact|vlm_event`, not the canonical EventCard-owned `event`.
Graph events explicitly retain participant node references; participant→event
is a registered dependency projection. An involves edge alone does not propagate.
Graph Event dependency references canonicalize to the EventCardSet-owned event;
its Graph node is not a second external event identity.

The existing character_state owner/projection is incomplete: it currently has
no resolvable object model. The production replacement defines Graph-owned
CharacterState with character_node_id, source_window_ref and nonempty state_fact_ids,
plus its stable local node ID. State facts must belong to that exact character,
observation window and an explicitly allowed state-fact kind. Never force arbitrary
action facts into state_fact_ids. A character may have an empty state_fact_ids list;
absence of state evidence produces no state object, not an empty accepted proof.
Window ownership is observation context, not proof that a state lasts the whole
window or remains the current state of the series. An independent character node
requires source entity references and identity evidence, with entity→character
dependency arcs. Until that evidence exists, retain the original person entity and
state Facts without inventing character/state nodes. Stage 2 can use an observed
person as a character-role view without minting a second identity; Stage 3 must
reject a required state query it cannot prove. These are required new wire models,
not functionality delivered by the reference slice.

## 4. Shared dependency algorithm

`semantic_chain/dependency_graph.py` computes already-projected graphs using
SemanticObjectRef exclusively. It does not infer Graph edges, source ownership,
policy completeness or an Admission decision.

- Require typed nodes, arcs and seeds; reject missing endpoints/roots.
- Canonical arc key is (from-ref JCS bytes, to-ref JCS bytes, kind,
  source-ref JCS bytes). Sort and deduplicate exact arcs.
- Compute SCCs without Python-recursion depth dependence.
- SCC ID is SHA256(JCS(node refs sorted by each ref's JCS bytes)).
- Build the condensation DAG and traverse deterministically.
- Seed reachability always retains its roots and explicitly projected external
  nodes; a root with no Graph descendants does not disappear.
- Preserve explicit unresolved frontier. Reaching all supplied nodes does not
  prove that the producer supplied every required projection.
- The upper evaluator independently reconstructs projections and evidence
  completeness before assigning bounded/unbounded or any KC rule status.

## 5. Required integration and verification

Reference/algorithm unit tests are not whole-Stage acceptance. The next compiler
wave must deliver the eight strict payload models, deterministic VLM/draft
projection, orthogonal conservation rows, structured conflict/evidence diagnostics,
Ledger-owned seeds, independent KC evaluators, and exact output reader.

Required negative cases include wrong owner with same object ID, missing or
ambiguous member, cross-set mixing, omitted standalone Fact/object/participant
projection, self-hash dependency, malformed or unsupported state evidence,
unresolved merge proposal and caller-mutated output containers.

The Command must re-decode audited raw draft bytes against Store-returned inputs,
not trust a public Python DTO as an admission token. Production policy values
must be explicit and input-bound. No ASR/VAD/physical endpoint enters Stage 1–3.

This workstation runs code checks only. Database upgrade/restart/replay and real
Doubao/SenseVoiceSmall/FSMN/whole-pipeline acceptance remain remote desktop work.

## 6. Implementation checkpoint

- member_refs.py and its 118 unit cases are implemented and independently
  reviewed (ALLOW), committed as bf88fb16.
- dependency_graph.py implements iterative SCC/condensation/seed reachability,
  not the Graph-to-arcs projector or completeness evaluator. Independent review
  is ALLOW. Its eight unit tests include a 1,200-node chain, an independent BFS
  oracle and all 512 three-node directed graphs: every root, SCC partition,
  SCC identity and condensation arcs.
- Combined selected semantic/VLM/Store/media/HTTP/runtime/architecture checks:
  554 passed, 6 skipped after the exhaustive oracle was added and independently
  delta-reviewed (ALLOW). Ruff and production BasedPyright pass. This is not
  whole-repository or PostgreSQL/model acceptance.
- Reproduce combined collection with
  `python -m pytest --import-mode=importlib -o 'pythonpath=packages/autocut-kernel/src tests/pipeline'`
  followed by the selected test paths. Default prepend-mode collection of HTTP
  and architecture tests together can resolve tests/tools instead of repository
  tools.architecture; importlib also requires the existing pipeline fixture path.
  The two collection failures were test-import failures, not a service startup
  or database failure. No production import path was altered to hide them.

The next delivery remains the actual eight output wire models/compiler,
independent KC evaluators and generation Command, followed by Stage 2/3 and
downstream Runtime integration. Do not register this algorithm as BuildNarrativeGraph.
