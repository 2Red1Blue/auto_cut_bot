# Calibration compatibility identity

## Decision

The old `service_sha256` is retained as an audit identity only.  It answers
which exact service bytes produced a measurement, but it is not by itself a
reason to invalidate a usable calibration.

Normal timed-speech admission instead compares a closed **timing compatibility
identity**.  It is derived from, and changes only with:

- profile schema and explicit timing-engine compatibility version;
- ASR/VAD model trees, model revisions, FunASR and Torch/CUDA runtime;
- execution device class and measured GPU capability when CUDA is used;
- decoder/resampling identity, native protocol identity, word timestamp and
  VAD merge policy identities; and
- exact ASR/VAD producer identities.

Any source change still creates a new `build_audit_sha256` in the profile.  A
build is compatible with an existing accepted record only when its recomputed
timing compatibility identity is equal.  A model, device, decoder, timestamp
or VAD-policy change is therefore fail-closed and requires a new calibration.
Changing logging, HTTP wording, health checks, unrelated API endpoints or VLM
policies changes only the audit identity and does not invalidate timed-speech.

The explicit timing-engine version is a protected compatibility promise.  A
change to source code that can alter emitted timings must bump it and therefore
forces a fresh calibration.  The deployment tool derives the compatibility
hash; callers cannot supply a claimed value.

## States

```text
current CUDA build + no accepted compatible record
    -> shadow-only service; calibration endpoints available
    -> normal timed-speech endpoint denied

current CUDA build + accepted compatible record + installed local-run authority
    -> normal timed-speech endpoint available to the Pipeline only
```

The service may start in shadow-only state; it must not become a normal service
by accepting a nonzero placeholder hash.  The Pipeline continues to resolve
the installed local-run authority from PostgreSQL before Media Preflight.

## Versioning

CPU grammars remain byte-compatible.  CUDA introduces separate shadow and
local-run profile schemas.  A CUDA profile contains the full build audit hash
and a derived timing compatibility hash.  It never widens the CPU decoder or
allows an arbitrary `device` string.

## PC execution boundary

The real Pipeline runs in WSL because SourcePrep requires POSIX directory-FD
safety.  CUDA inference remains in the hardened Podman service on loopback;
WSL calls only its authenticated HTTP endpoint.  The `D:` corpus is read as
`/mnt/d/...` by WSL and never copied into the repository.
