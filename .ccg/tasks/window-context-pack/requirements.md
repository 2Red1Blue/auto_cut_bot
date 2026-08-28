# WindowContextPack requirements

Only `WindowContextPack/v1` may reach a VLM prompt. API raw data, ASR/VAD,
subtitle text, shots and highlights remain out of model input. Bind a local
episode to an external episode explicitly, fail closed to a persisted
`video_only` pack, and bind exact selected context to VLM replay identity.
