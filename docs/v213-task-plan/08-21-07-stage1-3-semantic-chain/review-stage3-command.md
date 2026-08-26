# Stage3 durable Command review

Decision: accepted for the Kernel Command and exact replay slice. Runtime and
remote transaction/model acceptance remain separate, unfinished work.

BuildEditorialBlueprintCommand reads actual Stage1/2/root predecessors, freezes
the complete generation request, shares durable finite retry/reconciliation,
independently admits the whole target batch and submits exactly 3N+1 in one
Store operation. The reader returns actual stored members only after complete
raw/request/attempt audit and independent Admission recomputation.

Closed review findings: order all six batch checks by the registered closed
rule list, never a default-pass map; perform raw audit outside the semantic
rejection catch, so storage/audit corruption is not recorded as a model draft
error. Search exhaustion and joint intent infeasibility preserve full causal
Admission detail without partial business output or semantic transport retry.

Root and independent reviewer each ran 18 pure Command tests successfully,
covering a two-Story seven-member commit, exact replay, post-response/post-commit
crashes, unknown-outcome reconciliation, backoff/exhaustion, malformed semantic
output, actual audit corruption and fully rehashed false Admission. Ruff and
production typing passed. These in-memory tests do not prove PostgreSQL crash
atomicity or successful real Doubao generation; those remain remote acceptance.
