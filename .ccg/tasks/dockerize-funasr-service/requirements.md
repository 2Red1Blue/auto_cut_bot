# Dockerized FunASR service

## Scope

- Run one containerized CPU FunASR process that loads both SenseVoiceSmall and
  FSMN-VAD from host-mounted, read-only model snapshots.
- Keep the existing gateway's host port `8765` untouched.  Expose FunASR only
  on a configurable loopback host port, defaulting to `18765`.
- Keep secrets and the generated runtime profile outside Git.  Provide a
  tracked example environment file and a deterministic deployment command.
- Preserve the service's closed request/profile and single-model admission
  behavior.  The container is a deployment boundary, not a second VAD service.

## Acceptance criteria

- Docker Compose can build and start the FunASR service without modifying the
  gateway Compose topology.
- The service binds `0.0.0.0` only inside its isolated container; Docker maps
  it only to `127.0.0.1` on the host by default.
- Model directories are read-only mounts; no credentials, generated profile,
  or host-specific absolute path is tracked.
- Missing required deployment environment values fail before start.
- Automated tests cover the bind-host boundary and Compose configuration is
  validated without loading model weights.
