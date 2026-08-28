# Design review — Ark Responses SDK integration

## Result

The implementation design is **Go for Wave 1/2**, not a claim that the current
runtime has already been changed. No legacy commit will be cherry-picked.

## Closed in the design

- A default request scope is one complete endpoint plus an internal `default`
  account alias; it has no custom headers. Only a documented future endpoint
  contract may introduce allow-listed headers.
- SDK waiter remains the default simple path. A same-file-id retrieval state
  machine is required only for a future nonempty-header scope.
- Cache identity separates provider account/tenant, endpoint, source content
  and Files preprocessing without putting prompt/model information into a media
  cache.
- Unknown create outcomes remain quarantined and never authorize another upload.
- A later streaming-Blob port is constrained to immutable Store readers rather
  than arbitrary paths.

## Still external evidence, not guessed implementation

There is no evidence that the current Ark endpoint requires a tenant/project
header. Wave 1 therefore removes the misleading `tenant_id`/`project_id`
requirements rather than repurposing them. A future custom-header integration
must supply an endpoint contract and its own wire test.

## Required implementation tests

1. Every Files and Responses SDK call uses the same endpoint/account scope.
2. Default scope continues to use the SDK waiter; a nonempty-header scope is
   rejected unless it uses the explicit retrieve-only poller.
3. Different normalized path, credential scope or header scope cannot reuse a
   `file_id`; equal scope and policy uploads once.
4. Timeout while polling retrieves only the recorded `file_id`; it does not
   issue another create.
5. Logs, debug records and cache rows retain hashes only, never headers/API key.
6. Frozen SDK install and a real small-video wire run pass before batch use.
