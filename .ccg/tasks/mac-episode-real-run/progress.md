# Real execution, no synthetic success

Current code 70d90288. mac-local-run private launch uses normal pipeline-serve,
loopback18767, actual Podman ac_postgres/autocut. Only authorized episode1 is in
this semantic_only dataset; no claim for all50, ASR, story, render or publication.

1. Original v3 run `pipeline_run_1af0f6ea9de849e5ad4ecda470de300a`:
   SourcePrep succeeded, VLM failed PROVIDER_RESPONSE_INCOMPLETE/length;
   failure Receipt f9c0222e-556a-47a0-a707-e08549dee243 preserved. One attempt.
2. c16c494e runtime restarted. Same idempotency key returns the original v3
   failed run without changing its frozen profile to v4.
3. New v4 run `pipeline_run_18ac0863c1894ac5ae3c0eebb0804620`:
   SourcePrep succeeded, Receipt b512a49e-a792-4168-9ad5-9e447903787c.
   VLM response ID resp_0217878572748224ca67e9480bca887386e68bd4f0aad86263478;
   one attempt ended PROVIDER_RESPONSE_INCOMPLETE/length. Failed VLM Receipt
   c8d52444-4a36-4671-80b1-652556937458. Running same-key replay returned
   same run and keeps one attempt. New request uses same uploaded video reference
   as v3, with actual proxy time base1/12800 and9 frame anchors.

Do not blindly resubmit a failed/unknown provider operation. Check terminal/debug
and database state first. Private debug and launch paths in docs/mac-semantic-run-20260828.md.
No SSH, video or credentials in Git. New DB tests must never use real autocut.
Cross-Job selective recompute API and actual cross-machine handoff remain pending.

Second call metrics:input34413/output32768/reasoning26780/total67181; compact
body10973chars, one line. Representation improvement did not solve reasoning
budget exhaustion. Code task vlm-explicit-thinking passed independent review and
162 combined PostgreSQL profile/run-store tests, then committed as70d90288.

3rd run `pipeline_run_00cce9541d5546638ea69f3a9f8f86b9` now running:
- all oldworkers stopped before0029; fullbackup669MiB validated by native
  pg_restore --file=/dev/null (Podman stdin list emitted a disconnect afterTOC,
  so that listing alone was not treated as validation).
- existing5run fullrowchecksum remainsfb7b5128586535e0a5867c057c69e706,
  all5stillpassSQLvalidator; migrationcontainsnohistoricalrowrewrite.
- newprofilehash sha256:fb92757b38d73c4a5f42a12fdf134ca4078144d2f665f3023ce3dc85395cee5e.
- SourcePrep successReceipt98fc6a4c-4ddc-48b5-a557-42a6272046d5.
- actualproviderrequest thinking.type=disabled,32768tokens,stream=true;
  same exact uploadedvideo reference as secondcall.
- provider resp_021787858370633b7d8c272fa1fe35aa5baeadeea4f72cccea670,
  singleAttempt, stillawaitingterminal; notyetclaimedasVLMsuccess.
