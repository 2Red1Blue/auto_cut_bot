# Implementation plan

Design: docs/vlm-video-support-v4-design.md. This is a corrective VLM protocol
change, not permission to rewrite old successful/failed history.

Layer 1, current work:
- Worker semantic_resume_fix owns only new vlm/semantic_support_v4.py and
  tests/vlm/test_semantic_support_v4.py: typed support, exact millisecond clock,
  reversible frame aliases and negative tests. No old parser source edits.
- Main owns design/task tracking, raw-result diagnosis, complete-pack integration
  decisions. Reviewer independently checked B+C versus another prompt-only retry.
- Read-only impact scout identifies generation/finalizer/reader version dispatch.

Layer 2 ownership (current):
- semantic_resume_fix: Store models/postgres, new store/vlm_v4.py, Batch finalizer,
  real disposable PostgreSQL integration and negative tests.
- vlm_reuse_identity_impl: V4 pack/parser, profile models/reuse identity,
  incremental migration 0030 and new profile tests.
- Main: prompt/schema/factory/provider, Generation parser dispatch, Runtime
  scheduling/version selection, authority activation, docs and real HTTP run.
- recompute_design_review: read-only independent review, no provider/DB writes.

Checkpoint: V4 support and pack fixtures passed; first real disposable-DB
SourcePrep→Generation→Batch→fresh-Store/replay test passed. Review found outward
rounding falsely made adjacent millisecond segments overlap. Fix uses original
integer milliseconds for semantic relations; coarse PTS remains localization
only. New authority stays inactive until that regression and Store closure pass.

Next dependent layers (do not call real provider before these close):
1. New complete-pack parser and persisted decoder; preserve actual raw hash.
2. Generation parse/replay and versioned Batch ownership/consumer dispatch.
3. Model wire prompt/schema/factory + explicit authority/reuse identities.
4. Offline fixtures + real disposable PostgreSQL replay; independent review.
5. Commit and start one new real episode, retaining prior failures/debug.

No shared-file multiple writers, Claude, SSH, new deployment gates, fake frame
proofs, automatic timestamp repairs or stale-profile relabeling. Any unsupported
downstream consumer must be explicit; a semantic-only run is not full pipeline.
