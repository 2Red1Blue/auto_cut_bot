# Requirements

- Prefer root-cause replacement over compatibility patches.  A defect that
  crosses two or more stage boundaries must be fixed at the owning upstream
  contract before downstream implementation resumes.
- Replace the coarse VLM v2 observation list with one closed v3 semantic pack
  that carries local entities, visible facts, events, continuity and optional
  highlight/hook hypotheses.  Provider output uses integer proxy PTS and frame
  evidence only; Kernel owns source mapping, identities and provenance.
- Source preparation must commit a content-bound operation grant.  Stage 1
  requires semantic-analysis authority and Stages 2/4 independently require
  local-render-source authority.  Missing purpose is deny; a Candidate is not
  a reusable authorization token.
- Consume exact committed Source, Window, Doubao VLM, and frozen policy inputs;
  caller dictionaries, paths, hashes without Store membership, and latest-head
  lookup are not authorities.
- Persist Stage 1, Stage 2, and Stage 3 business members plus their sole
  evaluator-owned Admission in one Receipt/ArtifactSet transaction per command.
- Preserve VLM-only semantic ownership. Transcript, VAD, frames, samples, and
  physical edit endpoints remain unreachable until Stage 4.
- Replay and recovery return the original exact Receipt/ArtifactSet and do not
  invoke the provider or mint a second business set.
- Pipeline and Agent runtimes call the same shared commands and produce the same
  business hashes for identical committed inputs.
- Prove one real committed drama episode reaches an admitted Blueprint before
  starting the production Stage 4 command.
- Do not dual-write or translate v2 observations into v3 authority.  Historical
  v2 rows remain audit-only and every new semantic reader rejects them.
- Remove the old fixture command, VLM semantic adapter and Stage 1-3 prototype
  only after the replacement vertical slice passes PostgreSQL replay,
  conformance and adversarial tests; perform the consumer switch and deletion
  in the same migration wave so two authorities never coexist.
