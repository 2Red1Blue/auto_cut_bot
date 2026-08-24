-- Make the exact retry delay a durable, immutable part of each Attempt.

BEGIN;

ALTER TABLE runtime.generation_attempts
    ADD COLUMN retry_backoff_seconds integer NOT NULL DEFAULT 0,
    ADD CONSTRAINT generation_attempt_retry_backoff_nonnegative
        CHECK (retry_backoff_seconds >= 0),
    ADD CONSTRAINT generation_attempt_retry_schedule_exact CHECK (
        not_before_at = reserved_at
            + make_interval(secs => retry_backoff_seconds)
        AND (attempt_ordinal > 1 OR retry_backoff_seconds = 0)
    );

ALTER TABLE runtime.generation_attempts
    ALTER COLUMN retry_backoff_seconds DROP DEFAULT;

CREATE OR REPLACE FUNCTION runtime.prevent_generation_retry_schedule_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.retry_backoff_seconds IS DISTINCT FROM OLD.retry_backoff_seconds
       OR NEW.not_before_at IS DISTINCT FROM OLD.not_before_at THEN
        RAISE EXCEPTION 'generation retry schedule is immutable';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_generation_retry_schedule_no_mutation
BEFORE UPDATE ON runtime.generation_attempts
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_generation_retry_schedule_mutation();

COMMIT;
