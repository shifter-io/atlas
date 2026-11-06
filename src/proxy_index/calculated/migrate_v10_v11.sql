-- Transactional calculated v10 → v11 migration (CALC-PULSE-1a), applied by storage.migrate()
-- after the earlier calculated steps: the per-hour geo rollups, empty (schema.sql describes them;
-- the next calculation fills them). Nothing else changes; store.V10_CHECKS compares every
-- calculated_lists row with the copy taken by store.V10_SNAPSHOT.
CREATE TABLE calculated_geo_hours (
 hour timestamptz NOT NULL, scope_id integer, product_id integer NOT NULL,
 location_id integer NOT NULL, network_id integer NOT NULL, fallback text, target text NOT NULL,
 run_key bigint, completed bigint NOT NULL, successes bigint NOT NULL, challenges bigint NOT NULL,
 blocks bigint NOT NULL, light_ms bigint[] NOT NULL, light_n bigint[] NOT NULL
);
CREATE INDEX calculated_geo_hours_hour ON calculated_geo_hours(hour);
CREATE TABLE calculated_geo_rollups (
 hour timestamptz PRIMARY KEY, through_run bigint NOT NULL
);
