# Review

## Verified

- `tests/pipeline/test_funasr_timed_speech.py`: 26 passed.
- Ruff passes for the changed service and test files.
- `podman compose -f deploy/funasr/compose.yml config` expands successfully
  using `.env.example` values.
- The Compose stack mounts both model snapshots read-only, publishes only
  `127.0.0.1:18765` by default, and retains the root gateway's port `8765`.
- An invalid `FUNASR_BIND_HOST` is rejected before `Service()` can construct a
  model.

## Deferred desktop evidence

The current laptop has high swap pressure and no Docker CLI, so it did not
build the large locked FunASR/PyTorch image or load model weights.  The target
desktop must run the documented `docker compose ... up --build -d` command,
then retain the ready response and one authenticated bounded-media smoke as
the real-model deployment evidence.

## Existing static-analysis debt

`basedpyright` reports 161 errors in the pre-existing untyped JSON parsing
areas of `deploy/funasr/service.py`; the binding change introduces none of
those diagnostics.  This deployment change is covered by focused tests and
Ruff, but that service should be typed in a separate scoped task.
