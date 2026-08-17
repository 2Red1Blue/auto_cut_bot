# JSON Artifact Inventory — when-lucifer-kneels

Total files: 33

---


## asr-transcript.json (6.4KB)
- top-level keys (6): [schema_version, asr_endpoint, status, error, results, summary]
- structure:
    - schema_version: string ("1.0")
    - asr_endpoint: string ("http://localhost:10095/recognition")
    - status: string ("unavailable")
    - error: string ("ASR endpoint http://localhost:10095/recognition is...")
    - results: array (empty)
    - summary: array (45 items), first: object (5 keys)

## asr_anchor_results.json (1016.7KB)
- top-level keys (3): [schema_version, model, episodes]
- structure:
    - schema_version: string ("1.0")
    - model: string ("SenseVoiceSmall+fsmn-vad")
    - episodes: object
        - 1: object (13 keys)
        - 2: object (13 keys)
        - 3: object (13 keys)
        - 4: object (13 keys)
        - 5: object (13 keys)
        - 6: object (13 keys)
        - 7: object (13 keys)
        - 8: object (13 keys)
        - 9: object (13 keys)
        - 10: object (13 keys)

## audio-fix-report.json (36.0KB)
- top-level keys (3): [params, stats, changes]
- structure:
    - params: object
        - snap_tolerance: number (0.5)
        - lead_in: number (0.3)
        - lead_out: number (0.0)
        - audio_max_shift: number (3.0)
        - noise_db: number (-25)
        - min_silence_duration: number (0.2)
    - stats: object
        - total_candidates: number (117)
        - visual_only_snaps: number (0)
        - audio_gate_skipped: number (79)
        - audio_safe_snapped: number (13)
        - no_change: number (25)
        - total_value_changes: number (13)
    - changes: array (92 items), first: object (12 keys)

## chapter-digest-batch.json (9.8KB)
- top-level keys (5): [schema_version, backend, cache_dir, jobs, runtime_metadata]
- structure:
    - schema_version: string ("1.0")
    - backend: string ("doubao")
    - cache_dir: string ("/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/...")
    - jobs: array (8 items), first: object (5 keys)
    - runtime_metadata: object
        - schema_version: string ("1.0")
        - finished_at: string ("2026-08-13T05:27:40.206193+00:00")
        - manifest: string ("/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/...")
        - backend: string ("doubao")
        - dry_run: boolean (False)
        - execution_policy: object (5 keys)
        - succeeded: number (8)
        - failed: number (0)
        - results: array (8 items)
        - failures: array (empty)

## chapter-digests.jsonl (132.9KB)
- format: JSONL (showing first line structure)
- top-level keys (10): [schema_version, chapter_id, episodes, summary, character_rollup, relationship_rollup, story_threads, fact_keys, event_ids, open_question_keys]
- structure:
    - schema_version: string ("1.0")
    - chapter_id: string ("chapter-001-006")
    - episodes: array (6 items), first: number (1)
    - summary: string ("三周年纪念日当天，人类女子Selene携带为丈夫Lucifer准备的天使造型吊坠项链前往大教堂赴约，...")
    - character_rollup: array (9 items), first: object (6 keys)
    - relationship_rollup: array (6 items), first: object (3 keys)
    - story_threads: array (4 items), first: object (5 keys)
    - fact_keys: array (7 items), first: string ("fact-001")
    - event_ids: array (50 items), first: string ("event-bc09ff8e5e20")
    - open_question_keys: array (6 items), first: string ("oq-001")

## confidence-report.json (33.5KB)
- top-level keys (3): [generated_at, global_summary, window_assessments]
- structure:
    - generated_at: string ("2026-08-13T13:24:37.572755+00:00")
    - global_summary: object
        - status: string ("action_required")
        - recommendation: string ("recommend_enrichment: 39/45 窗口触发补充, 强烈建议启用 ASR 和/或...")
        - total_windows: number (45)
        - high_confidence_windows: number (45)
        - low_confidence_windows: number (0)
        - enrichment_triggered_count: number (39)
        - asr_recommended: boolean (True)
        - any_hard_subtitles_missing: boolean (True)
        - any_boundary_issues: boolean (True)
        - any_character_issues: boolean (False)
    - window_assessments: array (45 items), first: object (13 keys)

## episode-digest-batch.json (41.1KB)
- top-level keys (5): [schema_version, backend, cache_dir, jobs, runtime_metadata]
- structure:
    - schema_version: string ("1.0")
    - backend: string ("doubao")
    - cache_dir: string ("/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/...")
    - jobs: array (45 items), first: object (6 keys)
    - runtime_metadata: object
        - schema_version: string ("1.0")
        - finished_at: string ("2026-08-13T05:12:34.161435+00:00")
        - manifest: string ("/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/...")
        - backend: string ("doubao")
        - dry_run: boolean (False)
        - execution_policy: object (5 keys)
        - succeeded: number (45)
        - failed: number (0)
        - results: array (45 items)
        - failures: array (empty)

## episode-digests.jsonl (76.1KB)
- format: JSONL (showing first line structure)
- top-level keys (15): [schema_version, episode, source_ids, window_ids, opening_state, ending_state, summary, characters, relationships, event_ids, story_thread_updates, facts, open_questions, highlight_candidate_ids, hook_candidate_ids]
- structure:
    - schema_version: string ("1.0")
    - episode: number (1)
    - source_ids: array (1 items), first: string ("source-001")
    - window_ids: array (1 items), first: string ("source-001-w001")
    - opening_state: string ("Selene身着绣金香槟色长款礼服，佩戴珍珠发冠、十字架项链与水滴耳坠，手持为Lucifer准备的银...")
    - ending_state: string ("Lucifer浑身浴血、六翼展开，站在被冲天金色神火包裹的十字架前，眼睁睁看着锁在十字架上的Sele...")
    - summary: string ("三周年纪念日当天，人类女子Selene带着为丈夫Lucifer准备的天使吊坠项链前往大教堂赴约，开门...")
    - characters: array (empty)
    - relationships: array (empty)
    - event_ids: array (9 items), first: string ("event-4bd70e431bc1")
    - story_thread_updates: array (empty)
    - facts: array (empty)
    - open_questions: array (empty)
    - highlight_candidate_ids: array (empty)
    - hook_candidate_ids: array (empty)

## event-cards.jsonl (305.9KB)
- format: JSONL (showing first line structure)
- top-level keys (13): [id, episode, source_id, source_ranges, summary, function, character_names, cause, effect, open_question, temporal_mode, candidate_ids, boundary_resolution]
- structure:
    - id: string ("event-bc09ff8e5e20")
    - episode: number (1)
    - source_id: string ("source-001")
    - source_ranges: array (1 items), first: object (3 keys)
    - summary: string ("Selene在与Lucifer的三周年纪念日当天，手持定制的天使造型项链作为礼物，面带笑意走在教堂长...")
    - function: string ("开篇铺垫与人物引入")
    - character_names: array (1 items), first: string ("Selene")
    - cause: string ("Selene与Lucifer的三周年纪念日到来，她准备了专属项链作为纪念礼物，前去赴约")
    - effect: string ("Selene握住门把手推开木门，准备进入圣殿见Lucifer")
    - open_question: string ("门后等待Selene的会是什么场景？")
    - temporal_mode: string ("present")
    - candidate_ids: array (1 items), first: string ("cand-k-001")
    - boundary_resolution: object
        - status: string ("single_observation")
        - member_ranges: array (1 items)

## failure.json (1.4KB)
- top-level keys (7): [schema_version, status, failed_at, stage, error_code, error, details]
- structure:
    - schema_version: string ("1.0")
    - status: string ("failed")
    - failed_at: string ("2026-08-14T09:10:28.909311+00:00")
    - stage: string ("story_catalog")
    - error_code: string ("stage_failed")
    - error: string ("run_batch 全部 17 个 job 失败 (0 succeeded, 17 failed)....")
    - details: object

## highlight-hook-catalog.json (176.9KB)
- top-level keys (3): [schema_version, immutable, candidates]
- structure:
    - schema_version: string ("1.0")
    - immutable: boolean (True)
    - candidates: array (117 items), first: object (24 keys)

## project.json (6.7KB)
- top-level keys (5): [schema_version, created_at, updated_at, stages, fulfillment]
- structure:
    - schema_version: string ("1.0")
    - created_at: string ("2026-08-09T13:21:19.234927+00:00")
    - updated_at: string ("2026-08-15T04:00:29.448847+00:00")
    - stages: object
        - source_metadata: object (4 keys)
        - source_script: object (4 keys)
        - source_windows: object (5 keys)
        - asr_transcript: object (4 keys)
        - window_analysis: object (7 keys)
        - reconciliation: object (4 keys)
        - event_cards: object (4 keys)
        - episode_digests: object (4 keys)
        - chapter_digests: object (4 keys)
        - series_registry_job: object (5 keys)
    - fulfillment: object
        - proposal_count: number (0)
        - primary_script_count: number (0)
        - selected_story_count: number (0)
        - status: string ("not_started")

## scene_boundaries.json (92.2KB)
- top-level keys (7): [schema_version, detector, threshold, min_scene_len, total_scenes, episodes, precision]
- structure:
    - schema_version: string ("1.2")
    - detector: string ("PySceneDetect")
    - threshold: number (30.0)
    - min_scene_len: number (1.0)
    - total_scenes: number (2030)
    - episodes: object
        - 1: array (95 items)
        - 2: array (90 items)
        - 3: array (69 items)
        - 4: array (63 items)
        - 5: array (86 items)
        - 6: array (46 items)
        - 7: array (69 items)
        - 8: array (69 items)
        - 9: array (64 items)
        - 10: array (60 items)
    - precision: object
        - 1: number (0.04)
        - 2: number (0.04)
        - 3: number (0.04)
        - 4: number (0.04)
        - 5: number (0.04)
        - 6: number (0.04)
        - 7: number (0.04)
        - 8: number (0.04)
        - 9: number (0.04)
        - 10: number (0.04)

## series-assignment-batch.json (86.8KB)
- top-level keys (5): [schema_version, backend, cache_dir, jobs, runtime_metadata]
- structure:
    - schema_version: string ("1.0")
    - backend: string ("doubao")
    - cache_dir: string ("/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/...")
    - jobs: array (8 items), first: object (6 keys)
    - runtime_metadata: object
        - schema_version: string ("1.0")
        - finished_at: string ("2026-08-13T05:56:01.105934+00:00")
        - manifest: string ("/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/...")
        - backend: string ("doubao")
        - dry_run: boolean (False)
        - execution_policy: object (5 keys)
        - succeeded: number (6)
        - failed: number (2)
        - results: array (6 items)
        - failures: array (2 items)

## series-bible.json (172.4KB)
- top-level keys (13): [schema_version, metadata, series_summary, characters, main_characters, entity_importance, relationships, facts, story_threads, thread_beats, open_questions, unresolved_identity_conflicts, coverage]
- structure:
    - schema_version: string ("1.4")
    - metadata: object
        - episode_count: number (45)
        - total_events: number (338)
    - series_summary: string ("本短剧讲述天界炽天使Lucifer与人类女子Selene相恋违反天条，被高阶天使Sariel拆散，L...")
    - characters: array (11 items), first: object (9 keys)
    - main_characters: array (8 items), first: string ("char-lucifer")
    - entity_importance: object
        - char-lucifer: object (2 keys)
        - char-selene: object (2 keys)
        - char-aurora: object (2 keys)
        - char-laila: object (2 keys)
        - char-sariel: object (2 keys)
        - char-eldon: object (2 keys)
        - char-hester: object (2 keys)
        - char-silver-angel: object (2 keys)
        - char-red-witch-coven: object (2 keys)
        - char-demon-forces: object (2 keys)
    - relationships: array (10 items), first: object (4 keys)
    - facts: array (8 items), first: object (3 keys)
    - story_threads: array (4 items), first: object (16 keys)
    - thread_beats: array (100 items), first: object (8 keys)
    - open_questions: array (8 items), first: object (4 keys)
    - unresolved_identity_conflicts: array (empty)
    - coverage: object
        - ingestion_coverage: object (5 keys)
        - narrative_coverage: object (3 keys)

## series-bible.md (89.7KB)
- format: Markdown
- first 5 lines:
  # 全剧 Story Bible
  
  > **元数据**（v1.3 审计栏，用于跨 run 溯源）
  >
  > - schema_version：1.4

## series-registry-admission.json (25.7KB)
- top-level keys (12): [schema_version, policy_version, status, source_registry_sha256, core_registry_sha256, quarantine_sha256, counts, characters, blocked_story_thread_ids, local_admission_actions, blocking_errors, request_signature]
- structure:
    - schema_version: string ("1.0")
    - policy_version: string ("series-registry-partial-admission-v1")
    - status: string ("ready")
    - source_registry_sha256: string ("2fdd292fea9ee20b07d792d1e086907ba9e2c36e6029b427a8...")
    - core_registry_sha256: string ("2fdd292fea9ee20b07d792d1e086907ba9e2c36e6029b427a8...")
    - quarantine_sha256: string ("dc6348887e9464918ccf0d45d3512265c041be980c3728431e...")
    - counts: object
        - character_count: number (11)
        - admitted_character_count: number (11)
        - quarantined_character_count: number (0)
        - quarantined_event_count: number (0)
        - admitted_story_thread_count: number (4)
        - blocked_story_thread_count: number (0)
    - characters: array (11 items), first: object (6 keys)
    - blocked_story_thread_ids: array (empty)
    - local_admission_actions: array (empty)
    - blocking_errors: array (empty)
    - request_signature: string ("5e456fc892e7b8393b8aec9bc84732f066245ada37e7fe7c4c...")

## series-registry-batch.json (0.6KB)
- top-level keys (4): [schema_version, backend, cache_dir, jobs]
- structure:
    - schema_version: string ("1.0")
    - backend: string ("doubao")
    - cache_dir: string ("/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/...")
    - jobs: array (1 items), first: object (5 keys)

## series-registry-preflight.json (1.2KB)
- top-level keys (13): [schema_version, policy_version, status, failure_codes, episode_count, chapter_count, event_count, event_episode_count, event_rich, episode_rollup_counts, chapter_rollup_counts, diagnosis, inputs]
- structure:
    - schema_version: string ("1.0")
    - policy_version: string ("series-registry-semantic-rollup-preflight-v1")
    - status: string ("pass")
    - failure_codes: array (empty)
    - episode_count: number (45)
    - chapter_count: number (8)
    - event_count: number (338)
    - event_episode_count: number (45)
    - event_rich: boolean (True)
    - episode_rollup_counts: object
        - characters: number (0)
        - relationships: number (0)
        - story_thread_updates: number (0)
        - facts: number (0)
        - open_questions: number (0)
    - chapter_rollup_counts: object
        - character_rollup: number (57)
        - relationship_rollup: number (53)
        - story_threads: number (30)
        - fact_keys: number (97)
        - open_question_keys: number (94)
    - diagnosis: null
    - inputs: object
        - episode_digests: object (2 keys)
        - chapter_digests: object (2 keys)
        - event_cards: object (2 keys)

## series-registry-quarantine.json (0.5KB)
- top-level keys (16): [schema_version, policy_version, source_registry_sha256, quarantined_character_ids, quarantined_event_ids, blocked_story_thread_ids, character_reasons, identity_findings, characters, events, relationships, facts, story_threads, open_questions, unresolved_identity_conflicts, quarantined_ids]
- structure:
    - schema_version: string ("1.0")
    - policy_version: string ("series-registry-partial-admission-v1")
    - source_registry_sha256: string ("2fdd292fea9ee20b07d792d1e086907ba9e2c36e6029b427a8...")
    - quarantined_character_ids: array (empty)
    - quarantined_event_ids: array (empty)
    - blocked_story_thread_ids: array (empty)
    - character_reasons: array (empty)
    - identity_findings: array (empty)
    - characters: array (empty)
    - events: array (empty)
    - relationships: array (empty)
    - facts: array (empty)
    - story_threads: array (empty)
    - open_questions: array (empty)
    - unresolved_identity_conflicts: array (empty)
    - quarantined_ids: array (empty)

## series-registry-validation.json (0.5KB)
- top-level keys (11): [schema_version, policy_version, ok, status, core_registry_sha256, admission_sha256, quarantine_sha256, quarantined_id_count, leaked_ids, errors, request_signature]
- structure:
    - schema_version: string ("1.0")
    - policy_version: string ("series-registry-partial-admission-v1")
    - ok: boolean (True)
    - status: string ("ready")
    - core_registry_sha256: string ("2fdd292fea9ee20b07d792d1e086907ba9e2c36e6029b427a8...")
    - admission_sha256: string ("6e97209d949d1e94c7355cb0740825d26c66fa541b951cfe89...")
    - quarantine_sha256: string ("dc6348887e9464918ccf0d45d3512265c041be980c3728431e...")
    - quarantined_id_count: number (0)
    - leaked_ids: array (empty)
    - errors: array (empty)
    - request_signature: string ("5e456fc892e7b8393b8aec9bc84732f066245ada37e7fe7c4c...")

## series-registry.json (57.2KB)
- top-level keys (9): [schema_version, language, series_summary, characters, relationships, facts, story_threads, open_questions, unresolved_identity_conflicts]
- structure:
    - schema_version: string ("1.3")
    - language: string ("zh")
    - series_summary: string ("本短剧讲述天界炽天使Lucifer与人类女子Selene相恋违反天条，被高阶天使Sariel拆散，L...")
    - characters: array (11 items), first: object (9 keys)
    - relationships: array (10 items), first: object (4 keys)
    - facts: array (8 items), first: object (3 keys)
    - story_threads: array (4 items), first: object (8 keys)
    - open_questions: array (8 items), first: object (4 keys)
    - unresolved_identity_conflicts: array (empty)

## silence_intervals.json (29.2KB)
- top-level keys (6): [schema_version, detector, noise_db, min_silence_duration, total_silence_intervals, episodes]
- structure:
    - schema_version: string ("1.0")
    - detector: string ("ffmpeg-silencedetect")
    - noise_db: number (-25)
    - min_silence_duration: number (0.2)
    - total_silence_intervals: number (321)
    - episodes: object
        - 7: array (empty)
        - 4: array (empty)
        - 2: array (empty)
        - 3: array (3 items)
        - 1: array (2 items)
        - 6: array (1 items)
        - 8: array (1 items)
        - 5: array (1 items)
        - 9: array (empty)
        - 11: array (3 items)

## source_manifest.json (28.2KB)
- top-level keys (2): [schema_version, sources]
- structure:
    - schema_version: string ("1.0")
    - sources: array (45 items), first: object (6 keys)

## source_script.json (226.7KB)
- top-level keys (9): [schema_version, status, book_id, source_file_sha, episodes_detected, total_scenes, alignment_report, parse_metadata, episodes]
- structure:
    - schema_version: string ("1.0")
    - status: string ("ok")
    - book_id: string ("42000023011")
    - source_file_sha: string ("b34244866bdbfe46fc4cd21e635bf4975fae90ec8b9c5b29e5...")
    - episodes_detected: number (45)
    - total_scenes: number (100)
    - alignment_report: object
        - exact: number (156)
        - fuzzy: number (536)
        - inferred: number (1139)
        - none: number (4352)
        - total: number (6183)
        - alignment_rate: number (0.11191978004205079)
    - parse_metadata: object
        - attempts: number (1)
        - status: string ("success")
        - method: string ("two_pass")
        - total_episodes: number (45)
        - batch_size: number (10)
    - episodes: array (45 items), first: object (3 keys)

## speech_intervals.json (51.3KB)
- top-level keys (4): [schema_version, detector, vad_backend, episodes]
- structure:
    - schema_version: string ("1.0")
    - detector: string ("asr-anchor-sensevoice")
    - vad_backend: string ("asr_anchor")
    - episodes: object
        - 1: array (17 items)
        - 2: array (20 items)
        - 3: array (13 items)
        - 4: array (20 items)
        - 5: array (28 items)
        - 6: array (10 items)
        - 7: array (7 items)
        - 8: array (13 items)
        - 9: array (14 items)
        - 10: array (17 items)

## story-catalog-batch.json (226.5KB)
- top-level keys (4): [schema_version, backend, cache_dir, jobs]
- structure:
    - schema_version: string ("1.0")
    - backend: string ("doubao")
    - cache_dir: string ("/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/...")
    - jobs: array (17 items), first: object (8 keys)

## story-subarc-options.json (155.3KB)
- top-level keys (9): [schema_version, story_granularity, compiler_version, coverage_contract, required_thread_beat_ids, non_coda_thread_beat_ids, all_thread_beat_ids, recommended_option_ids, options]
- structure:
    - schema_version: string ("1.0")
    - story_granularity: string ("broad")
    - compiler_version: string ("coverage-first-broad-subarc-v2-typed-coda")
    - coverage_contract: object
        - required_thread_beat_coverage_ratio: number (1.0)
        - non_coda_thread_beat_coverage_ratio: number (0.85)
        - ordinary_story_min_beats: number (4)
        - ordinary_story_preferred_max_beats: number (8)
        - ordinary_story_hard_max_beats: number (12)
        - short_story_types: array (2 items)
    - required_thread_beat_ids: array (96 items), first: string ("beat-ep13-family-search-setup")
    - non_coda_thread_beat_ids: array (94 items), first: string ("beat-ep13-family-search-setup")
    - all_thread_beat_ids: array (100 items), first: string ("beat-ep13-family-search-setup")
    - recommended_option_ids: array (17 items), first: string ("subarc-0f7a227dd2c9")
    - options: array (38 items), first: object (18 keys)

## window-analysis-batch-1.json (0.8KB)
- top-level keys (3): [schema_version, backend, jobs]
- structure:
    - schema_version: string ("1.0")
    - backend: string ("doubao")
    - jobs: array (1 items), first: object (12 keys)

## window-analysis-batch-stream.json (0.8KB)
- top-level keys (3): [schema_version, backend, jobs]
- structure:
    - schema_version: string ("1.0")
    - backend: string ("doubao")
    - jobs: array (1 items), first: object (12 keys)

## window-analysis-batch.json (35.4KB)
- top-level keys (3): [schema_version, backend, jobs]
- structure:
    - schema_version: string ("1.0")
    - backend: string ("doubao")
    - jobs: array (45 items), first: object (12 keys)

## window-summaries.jsonl (1.1MB)
- format: JSONL (showing first line structure)
- top-level keys (11): [source_id, episode, window_id, window, window_summary, timeline_segments, boundary_context, story_beats, dialogue_and_text, visual_events, candidates]
- structure:
    - source_id: string ("source-001")
    - episode: number (1)
    - window_id: string ("source-001-w001")
    - window: object
        - start: number (0.0)
        - end: number (208.79)
    - window_summary: string ("三周年纪念日当天，人类女子Selene带着为丈夫Lucifer准备的天使吊坠项链前往大教堂赴约，开门...")
    - timeline_segments: array (1 items), first: object (6 keys)
    - boundary_context: object
        - starts_mid_scene: boolean (False)
        - ends_mid_scene: boolean (True)
        - continues_from_previous_window: boolean (False)
        - continues_into_next_window: boolean (True)
        - start_state: string ("Selene身着绣金香槟色长款礼服，佩戴珍珠发冠、十字架项链与水滴耳坠，手持为Lucifer准备的银...")
        - end_state: string ("Lucifer浑身浴血、六翼展开，站在被冲天金色神火包裹的十字架前，眼睁睁看着锁在十字架上的Sele...")
    - story_beats: array (9 items), first: object (8 keys)
    - dialogue_and_text: array (29 items), first: object (7 keys)
    - visual_events: array (15 items), first: object (8 keys)
    - candidates: array (4 items), first: object (14 keys)

## window_manifest.json (9.1KB)
- top-level keys (4): [schema_version, window_seconds, overlap_seconds, windows]
- structure:
    - schema_version: string ("1.0")
    - window_seconds: number (240)
    - overlap_seconds: number (12)
    - windows: array (45 items), first: object (7 keys)
