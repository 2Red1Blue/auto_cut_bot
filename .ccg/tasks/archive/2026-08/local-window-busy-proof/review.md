# Review and acceptance

Native implementation and independent native review: ALLOW. Root read the new
closed decoder and final client/service changes and independently reproduced
**195 passed, zero skipped**. Scope Ruff passed and Kernel/client BasedPyright
reported zero errors. The existing service as a whole is not type-clean (author
reports 163 pre-existing-region diagnostics); its new route/import had no
diagnostics. Do not represent the entire service as type-checked clean.

Only ServiceUnavailable from the narrow pre-dispatch admission await emits the
six-field canonical request/binding/profile-bound proof. Later/plain/malformed/
foreign/oversize 503 and incomplete transport results remain UNKNOWN. A bounded
transport may fail before returning status; the window client now preserves
that uncertainty rather than interpreting it as retry eligibility. No retry,
new key, Receipt or admission is introduced by this slice.

Ordinary worktree environment produced 166 passes and two missing-NumPy skips.
The reproducible no-skip run uses a temporary uv dependency overlay, without
changing project configuration or invoking real models/codecs/DB:

```sh
uv run --no-sync --with numpy==2.4.6 --with soundfile==0.14.0 \\
  python -m pytest --import-mode=importlib \\
  -o 'pythonpath=packages/autocut-kernel/src tests/pipeline' -q --tb=short \\
  tests/media/test_local_speech_window_busy.py \\
  tests/pipeline/test_funasr_window_http.py \\
  tests/pipeline/test_funasr_window_endpoint.py \\
  tests/pipeline/test_funasr_window_client_server.py \\
  tests/pipeline/test_funasr_timed_speech.py \\
  tests/pipeline/test_shadow_calibration_envelope_contract.py
```

HTTP uses an ephemeral loopback server with fake decoder/models; NumPy and
SoundFile cover synthetic PCM/FLOAT WAV. This is not real ASR or runtime proof.
