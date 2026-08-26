# Local window pre-dispatch BUSY evidence

Read-only review found that the local HTTP client currently labels every 503
TIMED_SPEECH_BUSY. Status alone does not establish that inference never began.
Task05's accepted retry design requires that distinction before a successor
attempt can be authorized. This slice adds wire evidence, not retries.

One worker owns new `media/local_speech_window_busy.py` in Kernel,
`auto_cut_bot/pipeline/media_preflight/funasr_window_http.py`, the window route
only in `deploy/funasr/service.py`, and the existing window HTTP/endpoint/
client-server tests plus new `tests/media/test_local_speech_window_busy.py`.
No changes to old full-source/shadow routes, queue algorithms, model startup,
calibration, Store, env/config, or other workers' source/audio mapper files.
Root owns this task and review/commits. No Claude/spawn, native codec/model/DB
execution or persistent services on Mac. Ephemeral aiohttp with fake decoder,
resource reader and fake model is permitted.

## Closed wire and error boundary

Shared frozen LocalSpeechWindowBusyProof contains request_sha256,
binding_sha256 and service_profile_sha256; schema_version is
local-speech-window-busy-v1, invocation_state is not_started and reason is
admission_busy. Serialization emits exactly these six fields. Decode bounded
raw bytes using existing strict JSON utilities, reject duplicates/coercions/
extra fields, compare every identity with the exact LocalSpeechWindowRequest,
and require canonical bytes. Expose a canonical hash; do not claim cryptographic
attestation or durable ownership from this DTO.

The trusted loopback/authenticated service may emit this body ONLY when
`window_evidence` has validated the request, policy, decoder and limits and
`await self.admit()` rejects before body materialization/native dispatch.
Catch ServiceUnavailable only around that admission call. Preserve 503 and
Retry-After where present. Never wrap later inference failures/cancellation in
not_started evidence. Not-ready 503 or unknown response stays unproven.
Keep body bounded by request/service response limits; if the proof cannot fit,
return an unproven bounded failure, not an oversized proof.

Client accepts 503 as retry-eligible evidence only after closed proof decoding.
Expose a typed LocalSpeechWindowBusyError (subclass of LocalMediaToolError)
with code TIMED_SPEECH_BUSY, exact proof and bounded original raw_response.
Unproven/malformed/foreign 503 gets TIMED_SPEECH_RESULT_UNKNOWN, no raw body or
credentials in diagnostics. No local retry, sleep or fresh key. Timeout and
disconnect remain unknown. A later claim-owned adapter will translate this
typed evidence into Kernel child outcome data; this is not yet a durable BUSY
Receipt or permission to advance attempt ordinal.

## Acceptance

Pure codec tests and real loopback HTTP + fake model verify: capacity/resource
busy yields exact proof with no model call; tampered request/profile/binding,
plain 503, duplicate/extra/wrong-state/oversize proof cannot authorize retry;
a post-dispatch 503 is unknown, not not_started; success and existing auth/
source/response bounds remain unchanged. Test no transparent retry and no
proxy/redirect behavior change. Scoped lint/types (do not claim the entire
existing native service is type-clean), independent review and scoped commit.

Followup remains mandatory: an exact failed-Receipt reader including prior
request/command/slot/receipt joins, bounded contiguous successor admission,
and shadow-local calibration before real local Runtime activation.
