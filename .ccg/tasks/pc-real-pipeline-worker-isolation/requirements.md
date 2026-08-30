# Requirements

1. A stale or no-longer-authorized historical Run must not terminate the process-wide worker.
2. The affected Run must retain an exact, durable cause and must not be silently treated as success.
3. Other pending Runs must remain schedulable after the historical Run is isolated.
4. Probe inspection must remain replay-safe; restarting with probe hold disabled must finalize the already-completed provider result without another paid call.
5. Add focused tests and a Trellis lifecycle rule preventing recurrence.
6. Commit and push the fix, update the clean PC worktree through Git, and verify the real one-episode pipeline reaches a terminal state.
