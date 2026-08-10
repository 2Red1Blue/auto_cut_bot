-- Migration: 001_provenance.sql
-- Purpose: Multi-source conflict resolution — provenance tracking and conflict
--          detection tables for the autocut pipeline.
-- Run:     psql -U autocut -d autocut -f 001_provenance.sql

CREATE TABLE IF NOT EXISTS autocut.source_provenance (
    id SERIAL PRIMARY KEY,
    entity_table TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field_path TEXT NOT NULL,
    values JSONB DEFAULT '{}',
    canonical_source TEXT DEFAULT '',
    resolved_at TIMESTAMPTZ DEFAULT now(),
    resolved_by TEXT DEFAULT 'auto_policy',
    UNIQUE (entity_table, entity_id, field_path)
);

CREATE TABLE IF NOT EXISTS autocut.source_conflicts (
    id SERIAL PRIMARY KEY,
    entity_table TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field_path TEXT NOT NULL,
    candidates JSONB DEFAULT '{}',
    severity TEXT DEFAULT 'low',
    status TEXT DEFAULT 'pending',
    resolution JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (entity_table, entity_id, field_path)
);

COMMENT ON TABLE autocut.source_provenance IS
'Multi-source provenance: records which source provided which value for each field.
Each row represents one field on one entity, with all source values in the values JSONB.';

COMMENT ON TABLE autocut.source_conflicts IS
'Multi-source conflict detection: when two sources disagree on a field value,
a conflict record is created. Conflicts are resolved by policy (auto) or manual review.';
