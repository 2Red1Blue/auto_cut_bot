-- ============================================================================
-- Migration: 002_schema_drift.sql
-- Purpose:  Reconcile schema drift — add columns that newer StageDBClient code
--           expects but that may be missing from databases created from older
--           schema dumps (e.g. ac_auto_cut's pre-existing tables).
--           Uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so it is idempotent
--           and safe to run repeatedly.
-- Run:      Applied automatically by db/migrate.py (versioned + tracked).
-- ============================================================================

-- ── subtitles: per-source text columns (StageDBClient.insert_subtitles) ──────
ALTER TABLE autocut.subtitles ADD COLUMN IF NOT EXISTS asr_text    text;
ALTER TABLE autocut.subtitles ADD COLUMN IF NOT EXISTS api_text    text;
ALTER TABLE autocut.subtitles ADD COLUMN IF NOT EXISTS script_text text;
