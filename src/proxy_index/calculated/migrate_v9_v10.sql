-- Transactional calculated v9 → v10 migration (contract K6), applied by storage.migrate() after
-- the earlier calculated steps: the incremental calculation's memo tables, empty (schema.sql
-- describes them). Nothing else changes; store.V9_CHECKS compares every calculated_lists row with
-- the copy taken by store.V9_SNAPSHOT.
CREATE TABLE calculated_run_ranges (
 run_key bigint NOT NULL, release_id integer NOT NULL, first_bucket timestamptz NOT NULL,
 last_bucket timestamptz NOT NULL, figures jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_key,release_id,first_bucket,last_bucket)
);
CREATE TABLE calculated_slot_figures (
 digest text PRIMARY KEY, figures jsonb NOT NULL, last_used timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE calculated_distinct (
 digest text PRIMARY KEY, counts jsonb NOT NULL, last_used timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE calculated_bucket_ips (
 bucket_start timestamptz PRIMARY KEY, runs_digest text NOT NULL, unique_ips bigint NOT NULL
);
CREATE TABLE calculated_ip_last_seen (
 ip_id bigint PRIMARY KEY, last_seen timestamptz NOT NULL
);
CREATE INDEX calculated_ip_last_seen_time ON calculated_ip_last_seen(last_seen);
