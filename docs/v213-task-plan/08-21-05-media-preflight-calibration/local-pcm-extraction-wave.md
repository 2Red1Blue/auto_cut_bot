# Local PCM extraction wave

This implements the first real native primitive in
[candidate-local I/O](candidate-local-io-followup.md), not a replacement root,
window Command, installed calibration, HTTP activation or production admission.
The existing full-source v1 protocol stays explicit and unchanged in this wave.

## Frozen split and ownership

- Root owns `media/local_audio_window.py`: strict source-bound extraction spec
  and a pure frame/slice tracker; no native decoder or model dependency.
- `calibration_contract` owns native service extraction, its dependency/container
  packaging, and synthetic decoder/model-input tests.
- `calibration_migration` owns pure tracker tests after the API is frozen.
- `review_calibration_migration` independently reviews the frozen diff.

The shared `LocalAudioWindowSpec` binds source identity, original audio clock
and extent, requested range, exact stream/rate/channels, committed boundary-set
hash, decoder identity and explicit source/frame/PCM work bounds. Construction
does not prove Store ownership or endpoint membership: the later window Command
must independently read and check the committed boundary set.

## Extraction semantics

1. Verify the original regular file hash/size against the explicit source cap.
   Native decoding uses that verified source, never an unverified caller proxy.
2. Decode the selected audio stream in order, without float seeking. Retain at
   most one decoded frame/conversion and a bounded output WAV, not an episode
   ndarray. Prefix decode cost remains and must be reported honestly.
3. Compute frame presentation as `pts * time_base` and sample positions as
   exact fractions. Reject missing/overlapping PTS, rate/channel changes, gaps
   in the requested window, non-integral sample cuts and exhausted limits.
   Frame-internal slices are mathematical sample positions, not new permission
   to select endpoints absent from the committed sample-boundary set.
4. Write only requested samples to FLOAT PCM WAV, preserving rate/channels.
   Convert integer/float sample formats explicitly, reject non-finite data;
   do not insert silence, resample or reset source-clock evidence. WAV local
   time zero is proved to correspond to the exact requested source start.
5. Finish only when continuous actual decoded coverage and written sample
   count equal the requested duration. Record source/spec/decoder and PCM/WAV
   hashes and counts. A direct report is not an accepted Artifact.
6. A service-native callable performs extraction and then invokes both models
   on the private local WAV within the existing serialized inference lifecycle.
   No new public endpoint or implicit opt-in through the old v1 request.

PyAV and its actual libav identities must be explicit; SoundFile/NumPy conversion
and shared planner code are part of extraction identity. Container packaging
includes the shared Kernel rather than an unaudited copy or sys.path injection.
Adding this native path changes service identity; old calibration cannot be
silently reused to approve it.

`max_frame_bytes` bounds each decoded plane total / largest conversion array,
not total process RSS: native frame storage, ndarray, FLOAT conversion and the
hashing block can coexist for one frame. No episode-wide PCM array is retained.
Service admission and the deployment memory limit remain separate controls.
Full-source hashing (before and after extraction) and prefix decoding remain
I/O/CPU costs; this wave reduces model input size, not source-transfer cost.

The Docker build installs the same-repository `packages/autocut-kernel` before
copying the service. A separately managed host environment must install that
package as well as `deploy/funasr/requirements.lock`; installing only the latter
no longer provides the shared planner. No model dependency enters the Kernel.

## Validation

Pure tests cover exact content-selection indices, nonzero/negative PTS, mixed
time bases, prefix discard, adjacent frames, internal gaps/overlap, fractional
sample rejection, truncated end and all resource bounds. Native tests use
synthetic decoder frames and model callbacks which inspect actual WAV samples;
they prove orchestration and conversion, not real MP4 decoder behavior.
Real encoded-source PyAV/native tests belong on the desktop, not this Mac.
No successful result of this wave grants calibration, Recipe or publication.

API references: [PyAV audio](https://pyav.basswood.io/docs/stable/api/audio.html),
[container decoding](https://pyav.basswood.io/docs/stable/api/container.html).
