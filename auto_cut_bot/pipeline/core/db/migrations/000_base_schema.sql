-- ============================================================================
-- Migration: 000_base_schema.sql
-- Purpose:  Primary 10-table schema for the autocut pipeline (books, subjects,
--           relationships, episodes, subtitles, speaker_mappings,
--           subject_episodes, shots, scenes, boundaries).
--           Plus the schema_migrations tracking table used by the auto-migrator.
-- Run:      Automatically applied at pipeline startup by db/migrate.py
--           (idempotent — safe to run repeatedly via CREATE ... IF NOT EXISTS).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS autocut;

-- ── Migration tracking table (used by the auto-migrator) ────────────────────
CREATE TABLE IF NOT EXISTS autocut.schema_migrations (
    version       text PRIMARY KEY,
    applied_at    timestamptz NOT NULL DEFAULT now()
);

-- ── 1. books ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.books (
    book_id          text PRIMARY KEY,
    book_name        text NOT NULL,
    total_episodes   integer,
    source_type      text NOT NULL DEFAULT 'vlm_only',
    overall_synopsis text,
    genre            text,
    sub_genre        text,
    mood             text,
    era              text,
    language         text DEFAULT 'zh',
    tags             jsonb DEFAULT '[]'::jsonb,
    script_parsed    jsonb,
    script_sha       text,
    script_raw_path  text,
    created_at       timestamptz DEFAULT now(),
    updated_at       timestamptz DEFAULT now()
);

-- ── 2. subjects ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.subjects (
    id              serial PRIMARY KEY,
    book_id         text NOT NULL REFERENCES autocut.books(book_id),
    name            text NOT NULL,
    aliases         jsonb DEFAULT '[]'::jsonb,
    persona         text,
    personality     jsonb DEFAULT '[]'::jsonb,
    traits          text,
    tone            text,
    voice_timbre    text,
    visual_features text,
    relationship    text,
    role            text,
    first_episode   integer,
    last_episode    integer,
    source          text NOT NULL DEFAULT 'vlm',
    vlm_verified    boolean DEFAULT false,
    vlm_verified_at timestamptz,
    created_at      timestamptz DEFAULT now(),
    sources_evidence jsonb DEFAULT '{}'::jsonb,
    UNIQUE (book_id, name)
);
CREATE INDEX IF NOT EXISTS idx_subjects_book ON autocut.subjects (book_id);

-- ── 3. relationships ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.relationships (
    id                 serial PRIMARY KEY,
    book_id            text NOT NULL REFERENCES autocut.books(book_id),
    source_subject_id  integer NOT NULL REFERENCES autocut.subjects(id),
    target_subject_id  integer NOT NULL REFERENCES autocut.subjects(id),
    description        text,
    source             text NOT NULL DEFAULT 'api',
    created_at         timestamptz DEFAULT now(),
    sources_evidence   jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT chk_no_self_relation CHECK (source_subject_id <> target_subject_id)
);
CREATE INDEX IF NOT EXISTS idx_relationships_book ON autocut.relationships (book_id);
CREATE INDEX IF NOT EXISTS idx_relationships_source ON autocut.relationships (source_subject_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON autocut.relationships (target_subject_id);

-- ── 4. episodes ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.episodes (
    episode_id       integer NOT NULL,
    book_id          text NOT NULL REFERENCES autocut.books(book_id),
    chapter_id       bigint,
    title            text,
    summary          text,
    is_free          boolean DEFAULT true,
    scene_count      integer,
    duration         real,
    source           text NOT NULL DEFAULT 'vlm',
    vlm_verified     boolean DEFAULT false,
    sources_evidence jsonb DEFAULT '{}'::jsonb,
    PRIMARY KEY (book_id, episode_id)
);

-- ── 5. subtitles ─────────────────────────────────────────────────────────────
-- NOTE: includes per-source text columns (asr_text/api_text/script_text) that
--       StageDBClient.insert_subtitles writes to. These were missing from older
--       schema dumps — this is the authoritative definition.
CREATE TABLE IF NOT EXISTS autocut.subtitles (
    id           serial PRIMARY KEY,
    book_id      text NOT NULL,
    episode_id   integer NOT NULL,
    start_time   real NOT NULL,
    end_time     real NOT NULL,
    speaker      text,
    text         text NOT NULL,
    tone         text,
    emotion      text,
    group_id     integer,
    group_tone   text,
    source       text NOT NULL DEFAULT 'api',
    confidence   real,
    cer_estimate real,
    asr_text     text,
    api_text     text,
    script_text  text,
    FOREIGN KEY (book_id, episode_id) REFERENCES autocut.episodes(book_id, episode_id),
    UNIQUE (book_id, episode_id, start_time)
);

-- ── 6. speaker_mappings ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.speaker_mappings (
    id                serial PRIMARY KEY,
    book_id           text NOT NULL REFERENCES autocut.books(book_id),
    episode_id        integer NOT NULL,
    speaker_label     text NOT NULL,
    mapped_subject_id integer REFERENCES autocut.subjects(id),
    confidence        real DEFAULT 0.0,
    resolved_by       text,
    resolved_at       timestamptz,
    created_at        timestamptz DEFAULT now(),
    UNIQUE (book_id, episode_id, speaker_label)
);
CREATE INDEX IF NOT EXISTS idx_speaker_mappings_book ON autocut.speaker_mappings (book_id, episode_id);

-- ── 7. subject_episodes ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.subject_episodes (
    id                serial PRIMARY KEY,
    subject_id        integer NOT NULL REFERENCES autocut.subjects(id),
    book_id           text NOT NULL,
    episode_id        integer NOT NULL,
    relationship      text,
    visual_features   text,
    appears_in_episode boolean DEFAULT true,
    source            text NOT NULL DEFAULT 'api',
    created_at        timestamptz DEFAULT now(),
    FOREIGN KEY (book_id, episode_id) REFERENCES autocut.episodes(book_id, episode_id),
    UNIQUE (subject_id, episode_id)
);

-- ── 8. shots ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.shots (
    id                serial PRIMARY KEY,
    book_id           text NOT NULL,
    episode_id        integer NOT NULL,
    start_time        real,
    end_time          real,
    scene             text,
    subjects          jsonb DEFAULT '[]'::jsonb,
    actions           text,
    is_highlight      boolean DEFAULT false,
    highlight_score   integer,
    highlight_reason  text,
    related_srt_range text,
    source            text NOT NULL DEFAULT 'api',
    FOREIGN KEY (book_id, episode_id) REFERENCES autocut.episodes(book_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_shots_time ON autocut.shots (book_id, episode_id, start_time, end_time);

-- ── 9. scenes ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.scenes (
    scene_id            text NOT NULL,
    book_id             text NOT NULL,
    episode_id          integer NOT NULL,
    scene_order         integer,
    heading             text,
    location            text,
    time_of_day         text,
    is_flashback        boolean DEFAULT false,
    flashback_label     text,
    characters_present  text[] DEFAULT '{}'::text[],
    dialogues           jsonb DEFAULT '[]'::jsonb,
    raw_description     text,
    distilled_summary   text,
    meta_tags           jsonb DEFAULT '{}'::jsonb,
    start_time          real,
    end_time            real,
    alignment_confidence text,
    alignment_source    text,
    source              text NOT NULL DEFAULT 'vlm',
    detected_in_video   boolean DEFAULT false,
    vlm_verified        boolean DEFAULT false,
    vlm_verified_at     timestamptz,
    PRIMARY KEY (book_id, scene_id),
    CONSTRAINT scenes_alignment_confidence_check CHECK (
        alignment_confidence = ANY (ARRAY['exact'::text, 'fuzzy'::text, 'inferred'::text, 'none'::text])
    )
);
CREATE INDEX IF NOT EXISTS idx_scenes_episode ON autocut.scenes (book_id, episode_id);
CREATE INDEX IF NOT EXISTS idx_scenes_time ON autocut.scenes (book_id, episode_id, start_time, end_time);

-- ── 10. boundaries ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autocut.boundaries (
    boundary_id  text NOT NULL,
    book_id      text NOT NULL,
    episode_id   integer NOT NULL,
    event_type   text NOT NULL,
    start_time   real NOT NULL,
    end_time     real NOT NULL,
    description  text,
    subjects     jsonb DEFAULT '[]'::jsonb,
    source_table text NOT NULL,
    source_id    text,
    confidence   text DEFAULT 'low',
    precision    real DEFAULT 2.0,
    verified_by  text[] DEFAULT '{}'::text[],
    corrected_at timestamptz,
    PRIMARY KEY (book_id, boundary_id),
    CONSTRAINT boundaries_confidence_check CHECK (
        confidence = ANY (ARRAY['high'::text, 'medium'::text, 'low'::text])
    )
);
CREATE INDEX IF NOT EXISTS idx_boundaries_time ON autocut.boundaries (book_id, episode_id, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_boundaries_type ON autocut.boundaries (book_id, event_type);
