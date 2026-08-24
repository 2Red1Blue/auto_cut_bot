# Implementation plan

1. Freeze typed exact-reference request and committed-reader result contracts.
2. Add Store readers that verify Receipt, ArtifactSet, scope, revision, member,
   blob length/hash/media type, and owner joins for Source/Window/VLM/policies.
3. Implement BuildNarrativeGraph command and atomic Stage 1 output set.
4. Implement CompileStoryPortfolio command and atomic Stage 2 output set.
5. Implement BuildEditorialBlueprint all-or-nothing command and atomic Stage 3
   output set.
6. Wire both runtimes only after the shared commands pass PostgreSQL replay and
   restart tests.
7. Run one real committed Doubao episode to admitted Blueprint, then perform two
   independent adversarial reviews and archive the task.

