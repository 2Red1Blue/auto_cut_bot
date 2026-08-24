# Requirements

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

