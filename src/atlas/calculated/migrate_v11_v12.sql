-- Transactional calculated v11 → v12 migration (CALC-PULSE-1b), applied by storage.migrate()
-- after the earlier calculated steps: the per-hour copies of the batch runs' buckets, empty
-- (schema.sql describes them; the next calculation fills them). Nothing else changes;
-- store.V11_CHECKS compares every calculated_lists row with the copy taken by store.V11_SNAPSHOT.
CREATE TABLE calculated_pulse_buckets (
 hour timestamptz NOT NULL, bucket_start timestamptz NOT NULL, run_key bigint NOT NULL,
 completed bigint NOT NULL, successes bigint NOT NULL, response_body_bytes bigint NOT NULL,
 errors jsonb NOT NULL, histogram_steps integer[] NOT NULL, histogram_counts integer[] NOT NULL,
 outcome_counters boolean NOT NULL, challenges bigint NOT NULL, blocks bigint NOT NULL,
 timeouts bigint NOT NULL, proxy_errors bigint NOT NULL, bytes_up bigint NOT NULL,
 bytes_down bigint NOT NULL, extra jsonb
);
CREATE INDEX calculated_pulse_buckets_hour ON calculated_pulse_buckets(hour);
CREATE TABLE calculated_pulse_rollups (
 hour timestamptz PRIMARY KEY, through_run bigint NOT NULL, loose bigint[] NOT NULL
);
